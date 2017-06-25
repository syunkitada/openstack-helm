#!/bin/bash -xe

echo 'Starting bootstrap'

mkdir -p /var/lib/nova/tmp
mkdir -p /var/lib/nova/instances
echo 'Success bootstrap'

[ -e /usr/bin/nova-rootwrap ] || ln -s /opt/nova/bin/nova-rootwrap /usr/bin/
[ -e /usr/bin/privsep-helper ] || ln -s /opt/nova/bin/nova-rootwrap /usr/bin/
chroot /host apt-get install -y qemu libvirt-bin python3-libvirt
chroot /host systemctl start libvirtd

# for module in `ls /usr/lib/python3/dist-packages/ | grep libvirt`
# do
#     ln -s /usr/lib/python3/dist-packages/$module /opt/nova/lib/python3.5/site-packages/
# done

echo 'Start nova-compute'
/opt/nova/bin/nova-compute --config-file /etc/nova/nova.conf
