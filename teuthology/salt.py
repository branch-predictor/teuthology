import logging
import re
import time

from cStringIO import StringIO
from netifaces import ifaddresses

import teuthology
from .contextutil import safe_while
from .misc import sh
from .orchestra.remote import Remote
from .orchestra import run

log = logging.getLogger(__name__)


class UseSalt(object):

    def __init__(self, machine_type, os_type):
        self.machine_type = machine_type
        self.os_type = os_type

    def openstack(self):
        if self.machine_type == 'openstack':
            return True
        return False

    def suse(self):
        if self.os_type in ['opensuse', 'sle']:
            return True
        return False

    def use_salt(self):
        if self.openstack() and self.suse():
           return True
        return False


class Salt(object):

    def __init__(self, ctx, config, **kwargs):
        self.ctx = ctx
        self.job_id = ctx.config.get('job_id')
        self.cluster = ctx.cluster
        self.remotes = ctx.cluster.remotes
        self.minions = []
        # FIXME: this seems fragile (ens3 hardcoded)
        teuthology_ip_address = ifaddresses('ens3')[2][0]['addr']
        ip_addr = teuthology_ip_address.split('.')
        teuthology_remote_name = "ubuntu@target{:03d}{:03d}{:03d}{:03d}.teuthology".format(
            int(ip_addr[0]),
            int(ip_addr[1]),
            int(ip_addr[2]),
            int(ip_addr[3]),
        )
        if(not config):
            self.master_remote = Remote(teuthology_remote_name)
        else:
            self.master_remote = Remote(config.get('master_remote',
                    teuthology_remote_name))
        log.debug("master_remote is {}".format(self.master_remote))

        self.__generate_minion_keys()
        self.__preseed_minions()
        self.__set_minion_master()
        self.__start_master()
        self.__start_minions()

    def __generate_minion_keys(self):
        '''
        Generate minion key on salt master to be used to preseed this cluster's
        minions.
        '''
        for rem in self.remotes.iterkeys():
            minion_id = rem.hostname
            self.minions.append(minion_id)
            log.debug("minion: ID {}".format(minion_id,))
            # mode 777 is necessary to be able to generate keys reliably
            # we hit this before: https://github.com/saltstack/salt/issues/31565
            self.master_remote.run(args = [
                'sudo',
                'sh',
                '-c',
                'if [ ! -d salt ]; then\
                mkdir -m 777 salt; fi'])
            self.master_remote.run(args = [
                'sudo',
                'sh',
                '-c',
                'if [ ! -d salt/minion-keys ]; then\
                mkdir -m 777 salt/minion-keys; fi'])
            self.master_remote.run(args = [
                'sudo',
                'sh',
                '-c',
                'if [ ! -f salt/minion-keys/{mid}.pem ]; then\
                salt-key --gen-keys={mid}\
                --gen-keys-dir=salt/minion-keys/; fi'.format(mid = minion_id)])

    def __cleanup_keys(self):
        '''
        Remove this cluster's minion keys (files and accepted keys)
        '''
        for rem in self.remotes.iterkeys():
            minion_id = rem.hostname
            log.debug("Deleting minion key: ID {}".format(minion_id))
            self.master_remote.run(args = ['sudo', 'salt-key', '-y', '-d',
                '{}'.format(minion_id)])
            self.master_remote.run(args = ['sudo', 'rm',
                'salt/minion-keys/{}.pem'.format(minion_id),
                'salt/minion-keys/{}.pub'.format(minion_id)])

    def __preseed_minions(self):
        '''
        Preseed minions with generated and accepted keys, as well as the job_id
        grain and the minion id (the remotes hostname)
        '''
        for rem in self.remotes.iterkeys():
            minion_id = rem.hostname
            self.master_remote.run(args = ['sudo', 'cp',
                'salt/minion-keys/{}.pub'.format(minion_id),
                '/etc/salt/pki/master/minions/{}'.format(minion_id)])
            self.master_remote.run(args = ['sudo', 'chown', 'ubuntu',
                "salt/minion-keys/{}.pem".format(minion_id),
                "salt/minion-keys/{}.pub".format(minion_id)])
            # copy the keys via the teuthology VM. The worker VMs can't ssh to
            # each other. scp -3 does a 3-point copy through the teuhology VM.
            sh('scp -3 {}:salt/minion-keys/{}.* {}:'.format(self.master_remote.name,
                minion_id, rem.name))
            r = rem.run(
                args=[
                    # add jobid to grains
                    'sudo',
		    'sh',
                    '-c',
                    'echo "grains:" > /etc/salt/minion.d/job_id_grains.conf;\
                    echo "  job_id: {}" >> /etc/salt/minion.d/job_id_grains.conf'.format(self.job_id),
                    # set proper owner and permissions on keys
		    'sudo',
                    'chown',
                    'root',
                    '{}.pem'.format(minion_id),
                    '{}.pub'.format(minion_id),
                    run.Raw(';'),
                    'sudo',
                    'chmod',
                    '600',
                    '{}.pem'.format(minion_id),
                    run.Raw(';'),
                    'sudo',
                    'chmod',
                    '644',
                    '{}.pub'.format(minion_id),
                    run.Raw(';'),
                    # move keys to correct location
                    'sudo',
                    'mv',
                    '{}.pem'.format(minion_id),
                    '/etc/salt/pki/minion/minion.pem',
                    run.Raw(';'),
                    'sudo',
                    'mv',
                    '{}.pub'.format(minion_id),
                    '/etc/salt/pki/minion/minion.pub',
                    run.Raw(';'),
                    # set minion id to hostname
                    'sudo',
                    'sh',
                    '-c',
                    'echo {} > /etc/salt/minion_id'.format(minion_id),
                ],
            )

    def __set_minion_master(self):
        """Points all minions to the master"""
        master_id = self.master_remote.hostname
        for rem in self.remotes.iterkeys():
            sed_cmd = 'echo master: {} > ' \
                      '/etc/salt/minion.d/master.conf'.format(master_id)
            rem.run(args=[
                # remove old master public key if present. Minion will refuse to
                # start if master name changed but old key is present
                'sudo',
                'rm',
                '/etc/salt/pki/minion/minion_master.pub',
                run.Raw(';'),
                # set master id
                'sudo',
                'sh',
                '-c',
                sed_cmd,
            ])

    def __start_master(self):
        """Starts salt-master.service on master_remote via SSH"""
        self.master_remote.run(args = ['sudo', 'systemctl', 'restart',
            'salt-master.service'])

    def __stop_minions(self):
        """Stops salt-minion.service on all target VMs"""
        self.cluster.run( args=['sudo', 'systemctl', 'stop',
            'salt-minion.service'])

    def __start_minions(self):
        """Starts salt-minion.service on all target VMs"""
        self.cluster.run( args=['sudo', 'systemctl', 'restart',
            'salt-minion.service'])

    def __ping(self, ping_cmd, expected):
        with safe_while(sleep=5, tries=10,
                action=ping_cmd) as proceed:
            while proceed():
                output = StringIO()
                self.master_remote.run(args = ping_cmd, stdout = output)
                responded = len(re.findall('True', output.getvalue()))
                log.debug("{} minion(s) responded".format(responded))
                output.close()
                if(expected == responded):
                    return

    def ping_minion(self, mid):
        """Pings a minion, raises exception if it doesn't respond"""
        self.__ping(["sudo", "salt", mid, "test.ping"], 1)

    def ping_minions(self):
        """Pings minions with this cluser's job_id, raises exception if they don't respond"""
        self.__ping(["sudo", "salt", "-C", "G@job_id:{}".format(self.job_id),
            "test.ping"], len(self.remotes))

