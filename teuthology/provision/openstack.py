import json
import logging
import os
import random
import re
import subprocess
import time
import tempfile
import openstack

from subprocess import CalledProcessError

from teuthology import misc

from teuthology.openstack import OpenStack, OpenStackInstance
from teuthology.config import config
from teuthology.contextutil import safe_while
from teuthology.exceptions import QuotaExceededError

# Use gevent's thread pool to run blocking subprocess operations
# This prevents gevent.exceptions.LoopExit when misc.sh() blocks in gevent greenlets
try:
    from gevent.threadpool import ThreadPool
    _subprocess_pool = ThreadPool(maxsize=10)
except ImportError:
    # Fallback if gevent is not available (shouldn't happen in normal operation)
    _subprocess_pool = None

def _threaded_sh(command, **kwargs):
    """
    Execute misc.sh() in a thread pool to avoid blocking gevent event loop.
    
    This prevents gevent.exceptions.LoopExit when misc.sh() is called
    from within a gevent greenlet. The blocking subprocess operation runs in a
    separate thread, allowing the gevent event loop to continue processing other greenlets.
    """
    if _subprocess_pool is not None:
        # Run in gevent thread pool - this doesn't block the event loop
        return _subprocess_pool.apply(misc.sh, (command,), kwargs)
    else:
        # Fallback if thread pool is not available (shouldn't happen in normal operation)
        return misc.sh(command, **kwargs)

log = logging.getLogger(__name__)


class ProvisionOpenStack(OpenStack):
    """
    A class that provides methods for creating and destroying virtual machine
    instances using OpenStack
    """
    def __init__(self):
        super(ProvisionOpenStack, self).__init__()
        fd, self.user_data = tempfile.mkstemp()
        os.close(fd)
        log.debug("ProvisionOpenStack: " + str(config.openstack))
        self.basename = 'target'
        self.up_string = 'The system is finally up'
        self.property = "%16x" % random.getrandbits(128)

    def __del__(self):
        if os.path.exists(self.user_data):
            os.unlink(self.user_data)

    def init_user_data(self, os_type, os_version):
        """
        Get the user-data file that is fit for os_type and os_version.
        It is responsible for setting up enough for ansible to take
        over.
        """
        template_path = config['openstack']['user-data'].format(
            os_type=os_type,
            os_version=os_version)
        log.info("Using user-data template: %s, %s, %s", template_path, os_type, os_version)
        nameserver = config['openstack'].get('nameserver', '8.8.8.8')
        user_data_template = open(template_path).read()
        user_data = user_data_template.format(
            up=self.up_string,
            nameserver=nameserver,
            username=self.username,
            lab_domain=config.lab_domain)
        open(self.user_data, 'w').write(user_data)

    def _openstack(self, subcommand, get=None):
        # do not use OpenStack().run because its
        # bugous for volume create as of openstackclient 3.2.0
        # https://bugs.launchpad.net/python-openstackclient/+bug/1619726
        #r = OpenStack().run("%s -f json " % command)
        json_result = _threaded_sh("openstack %s -f json" % subcommand)
        if 'No volume with a name or ID' in json_result:
            return json_result
        r = json.loads(json_result)
        if get:
            return self.get_value(r, get)
        return r

    def _create_volume(self, volume_name, size):
        """
        Create a volume and return valume id
        """
        volume_id = None
        try:
            volume_id = self._openstack("volume show %s" % volume_name, 'id')
        except subprocess.CalledProcessError as e:
            log.info("CalledProcessError % e.output %s", e, e.output)
            if 'No volume with a name or ID' not in e.output:
                raise e
        if volume_id:
            log.warning("Volume {} already exists with ID {}; using it"
                     .format(volume_name, volume_id))
        volume_id = self._openstack(
            "volume create %s" % config['openstack'].get('volume-create','')
            + " --property ownedby=%s" % config['openstack']['ip']
            + " --size %s" % str(size) + ' ' + volume_name, 'id')
        if volume_id:
            log.info("Volume {} created with ID {}"
                     .format(volume_name, volume_id))
            return volume_id
        else:
            raise Exception("Failed to create volume %s" % volume_name)

    def _await_volume_status(self, volume_id, status='available'):
        """
        Wait for volume to have status, like 'available' or 'in-use'
        """
        with safe_while(sleep=4, tries=50,
                        action="volume " + volume_id) as proceed:
            while proceed():
                try:
                    volume_status = \
                        self._openstack("volume show %s" % volume_id, 'status')
                    if volume_status == status:
                        break
                    else:
                        log.debug("volume %s not in '%s' status yet"
                                  % (volume_id, status))
                except subprocess.CalledProcessError:
                        log.warning("volume " + volume_id +
                                 " not information available yet")

    def _attach_volume(self, volume_id, name):
        """
        Attach volume to OpenStack instance.

        Try and attach volume to server, wait until volume gets in-use state.
        """
        with safe_while(sleep=20, increment=20, tries=3,
                        action="add volume " + volume_id) as proceed:
            while proceed():
                try:
                    _threaded_sh("openstack server add volume " + name + " " + volume_id)
                    break
                except subprocess.CalledProcessError:
                    log.warning("openstack add volume failed unexpectedly; retrying")
        self._await_volume_status(volume_id, 'in-use')

    def attach_volumes(self, server_name, volumes):
        """
        Create and attach volumes to the named OpenStack instance.
        If attachment is failed, make another try.
        """
        for i in range(volumes['count']):
            volume_name = server_name + '-' + str(i)
            volume_id = None
            with safe_while(sleep=10, tries=3,
                            action="volume " + volume_name) as proceed:
                while proceed():
                    try:
                        log.info("Calling _create_volume %s with size %s", volume_name, volumes['size'])
                        volume_id = self._create_volume(volume_name, volumes['size'])
                        self._await_volume_status(volume_id, 'available')
                        self._attach_volume(volume_id, server_name)
                        break
                    except Exception as e:
                        log.warning("%s" % e)
                        if volume_id:
                            OpenStack().volume_delete(volume_id)

    @staticmethod
    def ip2name(prefix, ip):
        """
        return the instance name suffixed with the IP address.
        """
        digits = map(int, re.findall(r'(\d+)\.(\d+)\.(\d+)\.(\d+)', ip)[0])
        return prefix + "%03d%03d%03d%03d" % tuple(digits)

    def create(self, num, os_type, os_version, arch, resources_hint):
        """
        Create num OpenStack instances running os_type os_version and
        return their names. Each instance has at least the resources
        described in resources_hint.
        """
        log.debug('ProvisionOpenStack:create')
        if arch is None:
            arch = self.get_default_arch()
        resources_hint = self.interpret_hints({
            'machine': config['openstack']['machine'],
            'volumes': config['openstack']['volumes'],
        }, resources_hint)
        self.init_user_data(os_type, os_version)
        image = self.image(os_type, os_version, arch)
        if 'network' in config['openstack']:
            net = "--nic net-id=" + str(self.net_id(config['openstack']['network']))
        else:
            net = ''
        flavor = self.flavor(resources_hint['machine'], arch)
        keypair = config['openstack']['keypair'] or 'teuthology'
        worker_group = config['openstack']['worker_group'] or 'teuthology-worker'
        cmd = ("flock --close --timeout 28800 /tmp/teuthology-server-create.lock" +
               " openstack server create" +
               " " + config['openstack'].get('server-create', '') +
               " -f json " +
               " --image '" + str(image) + "'" +
               " --flavor '" + str(flavor) + "'" +
               " --key-name %s " % keypair +
               " --user-data " + str(self.user_data) +
               " " + net +
               " --min " + str(num) +
               " --max " + str(num) +
               " --security-group %s" % worker_group +
               " --property teuthology=" + self.property +
               " --property ownedby=" + config.openstack['ip'] +
               " --wait " +
               " " + self.basename)
        try:
            status =self.run(cmd, type='compute')
            log.info("status is %s", status)
        except CalledProcessError as exc:
            if "quota exceeded" in exc.output.lower():
                raise QuotaExceededError(message=exc.output)
            raise
        raw_instances = list(self.list_instances())
        log.info("raw instances %s", raw_instances)

        filtered = filter(
            lambda instance: instance.get('Properties', {}).get('teuthology') == self.property,
            raw_instances
        )
        log.info("Matched instances: %s", filtered)

        # Create OpenStackInstance objects, handling ERROR state VMs gracefully
        instances = []
        error_instances = []
        for i in filtered:
            try:
                instance = OpenStackInstance(i['ID'], raise_on_error=True)
                instances.append(instance)
            except Exception as e:
                # If instance is in ERROR state, create it without raising to allow cleanup
                log.warning("Instance %s is in ERROR state, will be cleaned up: %s", i['ID'], e)
                error_instance = OpenStackInstance(i['ID'], raise_on_error=False)
                error_instances.append(error_instance)
                instances.append(error_instance)  # Include in list for cleanup
        
        log.info("IDs: %s (including %d ERROR state instances)", instances, len(error_instances))
        fqdns = []
        try:
            network = config['openstack'].get('network', '')
            log.info("networks: {}".format(network))
            # Process only non-ERROR instances (ERROR instances will be cleaned up in finally block)
            valid_instances = [inst for inst in instances if inst not in error_instances]
            for instance in valid_instances:
                ip = instance.get_ip(network)
                name = self.ip2name(self.basename, ip)
                self.run("server set " +
                         "--name " + name + " " +
                         instance['ID'])
                fqdn = f"ip-{ip.replace('.', '-')}.{config['lab_domain']}"
                if not misc.ssh_keyscan_wait(fqdn):
                    console_log = _threaded_sh("openstack console log show %s "
                                          "|| true" % instance['ID'])
                    log.error(console_log)
                    raise ValueError('ssh_keyscan_wait failed for ' + fqdn)
                time.sleep(15)
                if not self.cloud_init_wait(instance):
                    raise ValueError('cloud_init_wait failed for ' + fqdn)
                log.info("addressesssss  %s", ip)
                scp_cmd = (
                    f"scp -i /home/ubuntu/cephkey /home/ubuntu/cephkey "
                    f"ubuntu@{ip}:/home/ubuntu/.ssh/id_ed25519"
                )
                _threaded_sh(scp_cmd)
                ssh_cmd = (
                    f"ssh -i /home/ubuntu/cephkey ubuntu@{ip} "
                    f"\"chmod 600 ~/.ssh/id_ed25519 && chown ubuntu:ubuntu ~/.ssh/id_ed25519\""
                )
                _threaded_sh(ssh_cmd)
                log.info("Going to attach volumes")
                self.attach_volumes(name, resources_hint['volumes'])
                fqdns.append(fqdn)
        except Exception as e:
            log.exception(str(e))
            # Clean up all instances, including ERROR state ones
            for instance in instances:
                try:
                    instance_id = instance['ID'] if hasattr(instance, '__getitem__') else instance.name_or_id
                    self.destroy(instance_id)
                except Exception as destroy_err:
                    log.warning("Failed to destroy instance %s during cleanup: %s", 
                              instance_id if 'instance_id' in locals() else 'unknown', destroy_err)
            raise e
        finally:
            # Ensure ERROR state instances are always cleaned up, even if no exception occurred
            for error_instance in error_instances:
                try:
                    instance_id = error_instance['ID'] if hasattr(error_instance, '__getitem__') else error_instance.name_or_id
                    log.info("Cleaning up ERROR state instance: %s", instance_id)
                    self.destroy(instance_id)
                except Exception as destroy_err:
                    log.warning("Failed to destroy ERROR state instance %s: %s", 
                              instance_id if 'instance_id' in locals() else 'unknown', destroy_err)
        return fqdns

    def destroy(self, name_or_id):
        original = name_or_id

        # If it's a shortname, turn it into the *target* name first
        resolved = name_or_id
        m = re.match(r'^ip-(\d+)-(\d+)-(\d+)-(\d+)', name_or_id)
        if m:
            ip = ".".join(m.groups())
            prefix = config.openstack.get('name_prefix', 'target')  # e.g. "target"
            resolved = ProvisionOpenStack.ip2name(prefix, ip)       # -> target010000196186
            log.info("ProvisionOpenStack.destroy: converting %s -> %s (IP: %s)", 
                    original, resolved, ip)
        else:
            log.info("ProvisionOpenStack.destroy: using name as-is: %s", original)

        # Use openstacksdk to find the server (UUID) from the *target* name (or UUID)
        # Use raise_on_error=False to allow cleanup of ERROR state instances
        log.debug("ProvisionOpenStack.destroy: attempting to destroy %s", resolved)
        try:
            instance = OpenStackInstance(resolved, raise_on_error=False)
            result = instance.destroy()
            if result:
                log.info("ProvisionOpenStack.destroy: successfully destroyed %s", resolved)
            else:
                log.warning("ProvisionOpenStack.destroy: destroy() returned False/None for %s", resolved)
            return result
        except Exception as e:
            log.warning("ProvisionOpenStack.destroy: exception destroying %s: %s", resolved, e)
            # Try direct deletion via openstack CLI as fallback
            try:
                log.info("Attempting direct deletion via openstack CLI for %s", resolved)
                _threaded_sh(f"openstack server delete --wait {resolved} || true")
                return True
            except Exception as cli_err:
                log.error("ProvisionOpenStack.destroy: CLI deletion also failed for %s: %s", resolved, cli_err)
                return False
