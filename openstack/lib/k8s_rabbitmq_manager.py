# coding: utf-8

import os
import threading
import traceback
import time
from datetime import datetime

from flask import Flask
from influxdb import InfluxDBClient
from kombu import Connection, Exchange, Queue
from kubernetes import client, config as kube_config

from oslo_service import periodic_task
from oslo_config import cfg
from oslo_log import log
from oslo_service import service

import config, util

wsgi_app = Flask(__name__)

CONF = cfg.CONF
LOG = log.getLogger(__name__)

metrics_map = {}

STATUS_NOT_INSTALLED = -1
STATUS_INSTALLED = 0
STATUS_ACTIVE = 1
STATUS_ACTIVE_ALL_GREEN = 2

TEST_QUEUE_NAME = 'testqueue'
TEST_EXCHANGE_NAME = 'testex'
TEST_ROUTING_KEY = 'test.health'
TEST_MSG = 'hello'


class ServiceManager(service.Service):
    def __init__(self):
        super(ServiceManager, self).__init__()

    def start(self):
        LOG.info('start')

        if CONF.influxdb.enable:
            self.influxdb_periodic_tasks = InfluxdbPeriodicTasks()
            self.tg.add_dynamic_timer(self._get_influxdb_periodic_tasks,
                                      initial_delay=0,
                                      periodic_interval_max=120)

        if not CONF.rabbitmq_manager.enable_prometheus_exporter:
            self.prometheus_exporter_thread = self._spawn_prometheus_exporter()
        else:
            self.prometheus_exporter_thread = None

        self.periodic_tasks = ServicePeriodicTasks()
        self.tg.add_dynamic_timer(self._get_periodic_tasks,
                                  initial_delay=0,
                                  periodic_interval_max=120)

    def wait(self):
        LOG.info('wait')

    def stop(self):
        LOG.info('stop')

        if self.prometheus_exporter_thread is not None:
            self.prometheus_exporter_thread.join()

        super(ServiceManager, self).stop()

    def _get_periodic_tasks(self, raise_on_error=False):
        ctxt = {}
        return self.periodic_tasks.periodic_tasks(ctxt, raise_on_error=raise_on_error)

    def _get_influxdb_periodic_tasks(self, raise_on_error=False):
        ctxt = {}
        return self.influxdb_periodic_tasks.periodic_tasks(ctxt, raise_on_error=raise_on_error)

    def _spawn_prometheus_exporter(self):
        t = threading.Thread(target=wsgi_app.run, kwargs={
            'host': CONF.openstack_deploy_manager.bind_host,
            'port': CONF.openstack_deploy_manager.bind_port
        })
        t.daemon = True
        t.start()
        return t


#
# influxdb reporter
#
class InfluxdbPeriodicTasks(periodic_task.PeriodicTasks):
    def __init__(self):
        super(InfluxdbPeriodicTasks, self).__init__(CONF)
        self.influxdb = InfluxDBClient(
            CONF.influxdb.host,
            CONF.influxdb.port,
            CONF.influxdb.user,
            CONF.influxdb.password,
            CONF.influxdb.database,
        )

    def periodic_tasks(self, context, raise_on_error=False):
        return self.run_periodic_tasks(context, raise_on_error=raise_on_error)

    @periodic_task.periodic_task(spacing=60)
    def report(self, context):
        LOG.info('Report metrics to influxdb')
        json_body = []
        for measurement, metrics in metrics_map.items():
            json_body.append({
                "measurement": measurement.split(':')[0],
                "tags": metrics["tags"],
                "fields": {
                    "value": metrics["value"],
                }
            })

        if len(json_body) > 0:
            self.influxdb.write_points(json_body)


#
# prometheus exporter
#
@wsgi_app.route("/")
def status():
    return "OK"


@wsgi_app.route("/metrics")
def metrics():
    pmetrics = ''
    for measurement, metrics in metrics_map.items():
        labels = ''
        for k, v in metrics['tags'].items():
            labels += '{0}="{1}",'.format(k, v)
        labels = labels[:-1]
        pmetrics += '{0}{{{1}}} {2}\n'.format(measurement.split(':')[0], labels, metrics['value'])
    return pmetrics


#
# service tasks
#
class ServicePeriodicTasks(periodic_task.PeriodicTasks):
    def __init__(self):
        super(ServicePeriodicTasks, self).__init__(CONF)

        if os.path.exists('{0}/.kube/config'.format(os.environ['HOME'])):
            kube_config.load_kube_config()
        else:
            kube_config.load_incluster_config()
        self.k8s_corev1api = client.CoreV1Api()

        self.user = CONF.rabbitmq_manager.user
        self.password = CONF.rabbitmq_manager.password
        self.cluster_map = {}
        self.svc_map = {}
        self.helm_resource_map = {}
        self.k8s_services = {}
        self.k8s_pods = {}
        self.helm = util.Helm()

        self.update_resource_map()

        # initialize svc_map, and install rabbitmq-svc
        for name in CONF.rabbitmq_manager.services:
            cluster_name = 'rabbitmq-cluster-{0}'.format(name)
            svc_name = 'rabbitmq-svc-{0}'.format(name)
            self.init_cluster_data(cluster_name)
            self.svc_map[svc_name] = {
                'selector': cluster_name,
                'vhost': name,
            }

            if svc_name not in self.helm_resource_map:
                self.helm.install(svc_name, 'rabbitmq-svc')

        self.update_resource_map()

        for svc_name, svc in self.svc_map.items():
            k8s_svc = self.k8s_svc_map[svc_name]

            node_port = None
            for port in k8s_svc.spec.ports:
                if port.name == 'rabbitmq':
                    node_port = port.node_port
                    break

            transport_url = 'rabbit:\\\\/\\\\/'
            for node in self.rabbitmq_nodes:
                node_ip = None
                for address in node.status.addresses:
                    if address.type == 'InternalIP':
                        node_ip = address.address
                        break

                transport_url += "{0}:{1}@{2}:{3}\,".format(
                    self.user, self.password, node_ip, node_port
                )

            transport_url = transport_url[0:-2] + '\\\\/' + svc['vhost']
            svc['transport_url'] = transport_url
            option = "--set selector={0},transport_url='{1}'".format(svc['selector'], svc['transport_url'])
            self.helm.upgrade(svc_name, 'rabbitmq-svc', option)

    def update_resource_map(self):
        self.helm_resource_map = {}
        self.k8s_svc_map = {}
        self.k8s_pods_map = {}

        self.rabbitmq_nodes = self.k8s_corev1api.list_node(
            label_selector=CONF.rabbitmq_manager.node_label_selector).items

        if len(self.rabbitmq_nodes) < 1:
            raise Exception('rabbitmq-nodes are not found')

        self.helm_resource_map = self.helm.get_resource_map()

        k8s_svcs = self.k8s_corev1api.list_namespaced_service(
            CONF.k8s.namespace).items

        k8s_pods = self.k8s_corev1api.list_namespaced_pod(
            CONF.k8s.namespace).items

        for k8s_pod in k8s_pods:
            app_label = k8s_pod.metadata.labels.get('app')
            if app_label is None:
                continue
            pods = self.k8s_pods_map.get(app_label, [])
            pods.append(k8s_pod)
            self.k8s_pods_map[app_label] = pods

        for k8s_svc in k8s_svcs:
            name = k8s_svc.metadata.name
            self.k8s_svc_map[name] = k8s_svc

    def periodic_tasks(self, context, raise_on_error=False):
        return self.run_periodic_tasks(context, raise_on_error=raise_on_error)

    @periodic_task.periodic_task(spacing=10)
    def check(self, context):
        LOG.info('Start check')
        self.update_resource_map()

        for name, cluster in self.cluster_map.items():
            if name not in self.helm_resource_map:
                self.helm.install(name, 'rabbitmq-cluster')
                cluster['provisioning_status'] = STATUS_INSTALLED
                continue

            pods = 0
            running_pods = 0
            running_nodes = 0
            unhealty_pods = 0
            healty_pods = 0
            failed_get_cluster_status = 0
            partition_pods = 0
            is_healty = True
            is_alert = False
            for pod in self.k8s_pods_map[name]:
                pods += 1
                if not pod.status.phase == 'Running':
                    continue

                condition_status = True
                for condition in pod.status.conditions:
                    if condition.status != "True":
                        condition_status = False
                if not condition_status:
                    continue

                is_ready = True
                LOG.debug(pod.status)
                for cstatus in pod.status.container_statuses:
                    if not cstatus.ready:
                        is_ready = False
                        break
                if not is_ready:
                    unhealty_pods += 1
                    continue

                running_pods += 1

                cluster_status = self.get_cluster_status(pod)
                if cluster_status is None:
                    failed_get_cluster_status += 1
                    continue

                if cluster_status['is_partition']:
                    partition_pods += 1

                running_nodes += cluster_status['running_nodes']

                if self.test_queue(pod):
                    healty_pods += 1

            if partition_pods > 0:
                is_alert = True

            if not is_alert:
                if unhealty_pods != 0:
                    is_healty = False
                    cluster['warning']['exists_unhealty_pods'] += unhealty_pods
                    alert_threshold = CONF.rabbitmq_manager.wait_unhealty_pods_time / CONF.rabbitmq_manager.check_interval
                    if cluster['provisioning_status'] < STATUS_ACTIVE:
                        alert_threshold = alert_threshold * pods

                    LOG.warning('Found unhealty_pods={0}, alert_threshold={1}'.format(
                        cluster['warning']['exists_unhealty_pods'],
                        alert_threshold
                    ))
                    if cluster['warning']['exists_unhealty_pods'] >= alert_threshold:
                        is_alert = True
                else:
                    cluster['warning']['exists_unhealty_pods'] = 0

                    standalone_pods = (running_pods * running_pods) - running_nodes
                    if standalone_pods != 0:
                        is_healty = False
                        LOG.warning('Found standalone_pods')
                        cluster['warning']['exists_standalone_nodes'] += 1
                        if cluster['warning']['exists_standalone_nodes'] >= 2:
                            is_alert = True
                    else:
                        cluster['warning']['exists_standalone_nodes'] = 0

                    if failed_get_cluster_status != 0:
                        is_healty = False
                        LOG.warning('Failed get cluster_status')
                        cluster['warning']['failed_get_cluster_status'] += 1
                        if cluster['warning']['failed_get_cluster_status'] >= 4:
                            is_alert = True
                    else:
                        cluster['warning']['failed_get_cluster_status'] = 0

                    if is_healty:
                        cluster['provisioning_status'] = 1

            if is_alert:
                self.alert(name)

            metrics_map['rabbitmq_partition:' + name] = {
                'tags': {"deployment": name},
                'value': partition_pods,
            }

            metrics_map['rabbitmq_unhealty_pods:' + name] = {
                'tags': {"deployment": name},
                'value': unhealty_pods,
            }

            metrics_map['rabbitmq_healty_pods:' + name] = {
                'tags': {"deployment": name},
                'value': healty_pods,
            }

        LOG.info("Check Summary")
        for cluster_name, cluster in self.cluster_map.items():
            LOG.info("{0}: {1}".format(cluster_name, cluster))

    def get_cluster_status(self, pod):
        pod_name = pod.metadata.name
        cluster_status = util.execute('kubectl exec -n {0} {1} rabbitmqctl cluster_status'.format(
                                      CONF.k8s.namespace, pod_name),
                                      enable_exception=False)
        if cluster_status['return_code'] != 0:
            return None

        splited_msg = cluster_status['stdout'].split('{nodes', 1)
        splited_msg = splited_msg[1].split('{running_nodes,', 1)
        tmp_splited_msg = splited_msg[1].split('{cluster_name,', 1)
        if len(tmp_splited_msg) == 2:
            running_nodes = tmp_splited_msg[0]
            splited_msg = tmp_splited_msg[1].split('{partitions,', 1)
        else:
            splited_msg = splited_msg[1].split('{partitions,', 1)
            running_nodes = splited_msg[0]

        tmp_splited_msg = splited_msg[1].split('{alarms,', 1)
        if len(tmp_splited_msg) == 2:
            partitions = tmp_splited_msg[0]
        else:
            splited_msg = splited_msg[1].split('}]', 1)
            partitions = splited_msg[0]

        running_nodes_count = running_nodes.count('@')
        partitions_count = partitions.count('@')

        return {
            'running_nodes': running_nodes_count,
            'is_partition': (partitions_count > 0),
        }

    def test_queue(self, pod):
        connection = 'amqp://{0}:{1}@{2}:5672/test'.format(
            self.user, self.password, pod.status.pod_ip)
        exchange = Exchange(TEST_EXCHANGE_NAME, type='direct')
        queue = Queue(TEST_QUEUE_NAME, exchange=exchange, routing_key=TEST_ROUTING_KEY)
        is_success = False
        start = time.time()
        try:
            with Connection(connection) as c:
                bound = queue(c.default_channel)
                bound.declare()
                bound_exc = exchange(c.default_channel)
                msg = bound_exc.Message(TEST_MSG)
                bound_exc.publish(msg, routing_key=TEST_ROUTING_KEY)

                simple_queue = c.SimpleQueue(queue)
                msg = simple_queue.get(block=True, timeout=CONF.rabbitmq_manager.rpc_timeout)
                msg.ack()
                is_success = True
        except Exception:
            LOG.error(traceback.format_exc())

        if is_success:
            elapsed_time = time.time() - start
        else:
            elapsed_time = 25

        LOG.info("Latency: {0}".format(elapsed_time))
        timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        metrics_map['rabbitmq_msg_latency:'+pod.metadata.name] = {
            'tags': {"pod": pod.metadata.name, "deployment": pod.metadata.labels['app']},
            'value': elapsed_time,
            'time': timestamp,
        }

        return is_success

    def init_cluster_data(self, name):
        self.cluster_map[name] = {
            'provisioning_status': STATUS_NOT_INSTALLED,
            'warning': {
                'exists_unhealty_pods': 0,
                'exists_standalone_nodes': 0,
                'failed_get_cluster_status': 0,
            }
        }

    def alert(self, name):
        LOG.error("Alert {0}: {1}".format(name, self.cluster_map[name]))


def main():
    config.init()
    launcher = service.launch(CONF, ServiceManager())
    launcher.wait()


if __name__ == '__main__':
    main()
