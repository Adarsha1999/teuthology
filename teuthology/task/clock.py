"""
Clock synchronizer
"""
import logging
import contextlib

from teuthology.orchestra import run

log = logging.getLogger(__name__)

def filter_out_containers(cluster):
    """
    Returns a cluster that excludes remotes which should skip this task.
    Currently, only skips containerized remotes.
    """
    return cluster.filter(lambda r: not r.is_container)

@contextlib.contextmanager
def task(ctx, config):
    """
    Sync or skew clock

    This will initially sync the clocks.  Eventually it should let us also
    skew by some number of seconds.

    example::

        tasks:
        - clock:
        - ceph:
        - interactive:

    to sync.

    :param ctx: Context
    :param config: Configuration
    """

    log.info('Syncing clocks and checking initial clock skew...')
    cluster = filter_out_containers(ctx.cluster)
    # Fix ntp.conf before running ntpd (Ansible may have overwritten it)
    # Disable statistics to prevent permission errors that can cause ntpd -gq to hang
    run.wait(
        cluster.run(
            args=[
                'sudo', 'bash', '-c',
                'if [ -f /etc/ntp.conf ]; then '
                'sed -i "/^statsdir /d" /etc/ntp.conf; '
                'sed -i "/^filegen /d" /etc/ntp.conf; '
                'fi; '
                'if getent passwd ntp >/dev/null 2>&1; then '
                'mkdir -p /var/log/ntpstats && '
                'chown ntp:ntp /var/log/ntpstats 2>/dev/null || '
                'chown ntp:adm /var/log/ntpstats 2>/dev/null || '
                'chmod 775 /var/log/ntpstats 2>/dev/null || true; '
                'mkdir -p /var/lib/ntp && '
                'chown ntp:ntp /var/lib/ntp 2>/dev/null || '
                'chown ntp:adm /var/lib/ntp 2>/dev/null || '
                'chmod 755 /var/lib/ntp 2>/dev/null || true; '
                'fi'
            ],
            wait=False,
        )
    )
    run.wait(
        cluster.run(
            args = [
                'sudo', 'systemctl', 'stop', 'ntp.service', run.Raw('||'),
                'sudo', 'systemctl', 'stop', 'ntpd.service', run.Raw('||'),
                'sudo', 'systemctl', 'stop', 'chronyd.service',
                run.Raw(';'),
                'sudo', 'timeout', '30', 'ntpd', '-gq', run.Raw('||'),
                'sudo', 'chronyc', 'makestep',
                run.Raw(';'),
                'sudo', 'systemctl', 'start', 'ntp.service', run.Raw('||'),
                'sudo', 'systemctl', 'start', 'ntpd.service', run.Raw('||'),
                'sudo', 'systemctl', 'start', 'chronyd.service',
                run.Raw(';'),
                'PATH=/usr/bin:/usr/sbin', 'ntpq', '-p', run.Raw('||'),
                'PATH=/usr/bin:/usr/sbin', 'chronyc', 'sources',
                run.Raw('||'),
                'true'
            ],
            wait=False,
        )
    )

    try:
        yield

    finally:
        log.info('Checking final clock skew...')
        cluster = filter_out_containers(ctx.cluster)
        run.wait(
            cluster.run(
                args=[
                    'PATH=/usr/bin:/usr/sbin', 'ntpq', '-p', run.Raw('||'),
                    'PATH=/usr/bin:/usr/sbin', 'chronyc', 'sources',
                    run.Raw('||'),
                    'true'
                ],
                wait=False,
            )
        )


@contextlib.contextmanager
def check(ctx, config):
    """
    Run ntpq at the start and the end of the task.

    :param ctx: Context
    :param config: Configuration
    """
    log.info('Checking initial clock skew...')
    cluster = filter_out_containers(ctx.cluster)
    run.wait(
        cluster.run(
            args=[
                'PATH=/usr/bin:/usr/sbin', 'ntpq', '-p', run.Raw('||'),
                'PATH=/usr/bin:/usr/sbin', 'chronyc', 'sources',
                run.Raw('||'),
                'true'
            ],
            wait=False,
        )
    )

    try:
        yield

    finally:
        log.info('Checking final clock skew...')
        cluster = filter_out_containers(ctx.cluster)
        run.wait(
            cluster.run(
                args=[
                    'PATH=/usr/bin:/usr/sbin', 'ntpq', '-p', run.Raw('||'),
                    'PATH=/usr/bin:/usr/sbin', 'chronyc', 'sources',
                    run.Raw('||'),
                    'true'
                ],
                wait=False,
            )
        )
