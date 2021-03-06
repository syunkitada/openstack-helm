#!/bin/bash -xe
{{$mysql := .Values.mysql}}
{{$admin_password := .Values.openstack.admin_password}}

source /mnt/openstack/etc/adminrc

helm get openstack-mysql \
    || helm install {{ .Values.chart_prefix }}/mariadb \
        --name openstack-mysql --namespace {{ .Release.Namespace }} \
        --set persistence.enabled=false,mariadbRootPassword={{ $mysql.root_pass }} \
        -f /mnt/openstack/etc/values.yaml

sleep 10

# Setup db
{{range $database, $database_data := $mysql.database_map}}
{{range $dbname := $database_data.dbs}}
echo "Create db: $dbname"
mysql -h{{$database_data.host}} -P{{$database_data.port}} \
      -u{{$database_data.user}} -p{{$database_data.password}} -e 'CREATE DATABASE IF NOT EXISTS {{$dbname}}'
{{end}}
{{end}}
