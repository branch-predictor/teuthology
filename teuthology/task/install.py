import contextlib
import copy
import logging
import time
import os
import subprocess

from cStringIO import StringIO
from teuthology import misc as teuthology
from teuthology import contextutil
from teuthology.parallel import parallel
from ..orchestra import run

log = logging.getLogger(__name__)

# Should the RELEASE value get extracted from somewhere?
RELEASE = "1-0"

# This is intended to be a complete listing of ceph packages. If we're going
# to hardcode this stuff, I don't want to do it in more than once place.
rpm_packages = {'ceph': [
    'ceph',
    'ceph-debuginfo',
    'ceph-radosgw',
    'ceph-test',
    'ceph-devel',
    'ceph-fuse',
    'ceph-deploy',
    #'rest-bench',
    #'libcephfs_jni1',
    'libcephfs1',
    'python-ceph',
    'rbd-fuse',
    'python-radosgw-agent',
    'python-virtualenv',
]}

rpm_extras_packages = ['rbd-kmp-default','qemu-block-rbd','qemu-tools']

def _run_and_log_error_if_fails(remote, args):
    """
    Yet another wrapper around command execution. This one runs a command on
    the given remote, then, if execution fails, logs the error and re-raises.

    :param remote: the teuthology.orchestra.remote.Remote object
    :param args: list of arguments comprising the command the be executed
    :returns: None
    :raises: CommandFailedError
    """
    response = StringIO()
    try:
        remote.run(
            args=args,
            stdout=response,
            stderr=response,
        )
    except CommandFailedError:
        log.error(response.getvalue().strip())
        raise


def _get_config_value_for_remote(ctx, remote, config, key):
    """
    Look through config, and attempt to determine the "best" value to use for a
    given key. For example, given:

        config = {
            'all':
                {'branch': 'master'},
            'branch': 'next'
        }
        _get_config_value_for_remote(ctx, remote, config, 'branch')

    would return 'master'.

    :param ctx: the argparse.Namespace object
    :param remote: the teuthology.orchestra.remote.Remote object
    :param config: the config dict
    :param key: the name of the value to retrieve
    """
    roles = ctx.cluster.remotes[remote]
    if 'all' in config:
        return config['all'].get(key)
    elif roles:
        for role in roles:
            if role in config and key in config[role]:
                return config[role].get(key)
    return config.get(key)


def _get_uri(tag, branch, sha1):
    """
    Set the uri -- common code used by both install and debian upgrade
    """
    uri = None
    if tag:
        uri = 'ref/' + tag
    elif branch:
        uri = 'ref/' + branch
    elif sha1:
        uri = 'sha1/' + sha1
    else:
        # FIXME: Should master be the default?
        log.debug("defaulting to master branch")
        uri = 'ref/master'
    return uri


def _get_baseurlinfo_and_dist(ctx, remote, config):
    """
    Through various commands executed on the remote, determines the
    distribution name and version in use, as well as the portion of the repo
    URI to use to specify which version of the project (normally ceph) to
    install.Example:

        {'arch': 'x86_64',
        'dist': 'raring',
        'dist_release': None,
        'distro': 'Ubuntu',
        'distro_release': None,
        'flavor': 'basic',
        'relval': '13.04',
        'uri': 'ref/master'}

    :param ctx: the argparse.Namespace object
    :param remote: the teuthology.orchestra.remote.Remote object
    :param config: the config dict
    :returns: dict -- the information you want.
    """
    retval = {}
    relval = None
    r = remote.run(
        args=['arch'],
        stdout=StringIO(),
    )
    retval['arch'] = r.stdout.getvalue().strip()
    r = remote.run(
        args=['lsb_release', '-is'],
        stdout=StringIO(),
    )
    retval['distro'] = r.stdout.getvalue().strip()
    r = remote.run(
        args=[
            'lsb_release', '-rs'], stdout=StringIO())
    retval['relval'] = r.stdout.getvalue().strip()
    dist_name = None
    if retval['distro'] == 'CentOS':
        relval = retval['relval']
        relval = relval[0:relval.find('.')]
        distri = 'centos'
        retval['distro_release'] = '%s%s' % (distri, relval)
        retval['dist'] = retval['distro_release']
        dist_name = 'el'
        retval['dist_release'] = '%s%s' % (dist_name, relval)
    elif retval['distro'] == 'RedHatEnterpriseServer':
        relval = retval['relval'].replace('.', '_')
        distri = 'rhel'
        retval['distro_release'] = '%s%s' % (distri, relval)
        retval['dist'] = retval['distro_release']
        dist_name = 'el'
        short_relval = relval[0:relval.find('_')]
        retval['dist_release'] = '%s%s' % (dist_name, short_relval)
    elif retval['distro'] == 'Fedora':
        distri = retval['distro']
        dist_name = 'fc'
        retval['distro_release'] = '%s%s' % (dist_name, retval['relval'])
        retval['dist'] = retval['dist_release'] = retval['distro_release']
    else:
        r = remote.run(
            args=['lsb_release', '-sc'],
            stdout=StringIO(),
        )
        retval['dist'] = r.stdout.getvalue().strip()
        retval['distro_release'] = None
        retval['dist_release'] = None

    # branch/tag/sha1 flavor
    retval['flavor'] = config.get('flavor', 'basic')

    log.info('config is %s', config)
    tag = _get_config_value_for_remote(ctx, remote, config, 'tag')
    branch = _get_config_value_for_remote(ctx, remote, config, 'branch')
    sha1 = _get_config_value_for_remote(ctx, remote, config, 'sha1')
    uri = _get_uri(tag, branch, sha1)
    retval['uri'] = uri

    return retval


def _get_baseurl(ctx, remote, config):
    """
    Figures out which package repo base URL to use.

    Example:
        'http://gitbuilder.ceph.com/ceph-deb-raring-x86_64-basic/ref/master'
    :param ctx: the argparse.Namespace object
    :param remote: the teuthology.orchestra.remote.Remote object
    :param config: the config dict
    :returns: str -- the URL
    """
    # get distro name and arch
    baseparms = _get_baseurlinfo_and_dist(ctx, remote, config)
    base_url = 'http://{host}/{proj}-{pkg_type}-{dist}-{arch}-{flavor}/{uri}'.format(
        host=ctx.teuthology_config.get('gitbuilder_host',
                                       'gitbuilder.ceph.com'),
        proj=config.get('project', 'ceph'),
        pkg_type=remote.system_type,
        **baseparms
    )
    return base_url


class VersionNotFoundError(Exception):

    def __init__(self, url):
        self.url = url

    def __str__(self):
        return "Failed to fetch package version from %s" % self.url


def _block_looking_for_package_version(remote, base_url, wait=False):
    """
    Look for, and parse, a file called 'version' in base_url.

    :param remote: the teuthology.orchestra.remote.Remote object
    :param wait: wait forever for the file to show up. (default False)
    :returns: str -- the version e.g. '0.67-240-g67a95b9-1raring'
    :raises: VersionNotFoundError
    """
    while True:
        r = remote.run(
            args=['wget', '-q', '-O-', base_url + '/version'],
            stdout=StringIO(),
            check_status=False,
        )
        if r.exitstatus != 0:
            if wait:
                log.info('Package not there yet, waiting...')
                time.sleep(15)
                continue
            raise VersionNotFoundError(base_url)
        break
    version = r.stdout.getvalue().strip()
    return version

def _get_local_dir(config, remote):
    """
    Extract local directory name from the task lists.
    Copy files over to the remote site.
    """
    ldir = config.get('local', None)
    if ldir:
        remote.run(args=['sudo', 'mkdir', '-p', ldir,])
        for fyle in os.listdir(ldir):
            fname = "%s/%s" % (ldir, fyle)
            teuthology.sudo_write_file(remote, fname, open(fname).read(), '644')
    return ldir

def _update_deb_package_list_and_install(ctx, remote, debs, config):
    """
    Runs ``apt-get update`` first, then runs ``apt-get install``, installing
    the requested packages on the remote system.

    TODO: split this into at least two functions.

    :param ctx: the argparse.Namespace object
    :param remote: the teuthology.orchestra.remote.Remote object
    :param debs: list of packages names to install
    :param config: the config dict
    """

    # check for ceph release key
    r = remote.run(
        args=[
            'sudo', 'apt-key', 'list', run.Raw('|'), 'grep', 'Ceph',
        ],
        stdout=StringIO(),
        check_status=False,
    )
    if r.stdout.getvalue().find('Ceph automated package') == -1:
        # if it doesn't exist, add it
        remote.run(
            args=[
                'wget', '-q', '-O-',
                'https://ceph.com/git/?p=ceph.git;a=blob_plain;f=keys/autobuild.asc',
                run.Raw('|'),
                'sudo', 'apt-key', 'add', '-',
            ],
            stdout=StringIO(),
        )

    baseparms = _get_baseurlinfo_and_dist(ctx, remote, config)
    log.info("Installing packages: {pkglist} on remote deb {arch}".format(
        pkglist=", ".join(debs), arch=baseparms['arch'])
    )
    # get baseurl
    base_url = _get_baseurl(ctx, remote, config)
    log.info('Pulling from %s', base_url)

    # get package version string
    # FIXME this is a terrible hack.
    while True:
        r = remote.run(
            args=[
                'wget', '-q', '-O-', base_url + '/version',
            ],
            stdout=StringIO(),
            check_status=False,
        )
        if r.exitstatus != 0:
            if config.get('wait_for_package'):
                log.info('Package not there yet, waiting...')
                time.sleep(15)
                continue
            raise VersionNotFoundError("%s/version" % base_url)
        version = r.stdout.getvalue().strip()
        log.info('Package version is %s', version)
        break

    remote.run(
        args=[
            'echo', 'deb', base_url, baseparms['dist'], 'main',
            run.Raw('|'),
            'sudo', 'tee', '/etc/apt/sources.list.d/{proj}.list'.format(
                proj=config.get('project', 'ceph')),
        ],
        stdout=StringIO(),
    )
    remote.run(
        args=[
            'sudo', 'apt-get', 'update', run.Raw('&&'),
            'sudo', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', '--force-yes',
            '-o', run.Raw('Dpkg::Options::="--force-confdef"'), '-o', run.Raw(
                'Dpkg::Options::="--force-confold"'),
            'install',
        ] + ['%s=%s' % (d, version) for d in debs],
        stdout=StringIO(),
    )
    ldir = _get_local_dir(config, remote)
    if ldir:
        for fyle in os.listdir(ldir):
            fname = "%s/%s" % (ldir, fyle)
            remote.run(args=['sudo', 'dpkg', '-i', fname],)


def _yum_fix_repo_priority(remote, project, uri):
    """
    On the remote, 'priority=1' lines to each enabled repo in:

        /etc/yum.repos.d/{project}.repo

    :param remote: the teuthology.orchestra.remote.Remote object
    :param project: the project whose repos need modification
    """
    remote.run(
        args=[
            'sudo',
            'sed',
            '-i',
            '-e',
            run.Raw(
                '\':a;N;$!ba;s/enabled=1\\ngpg/enabled=1\\npriority=1\\ngpg/g\''),
            '-e',
            run.Raw("'s;ref/[a-zA-Z0-9_]*/;{uri}/;g'".format(uri=uri)),
            '/etc/yum.repos.d/%s.repo' % project,
        ]
    )


def _update_rpm_package_list_and_install(ctx, remote, rpm, config):
    """
    Installs the ceph-release package for the relevant branch, then installs
    the requested packages on the remote system.

    TODO: split this into at least two functions.

    :param ctx: the argparse.Namespace object
    :param remote: the teuthology.orchestra.remote.Remote object
    :param rpm: list of packages names to install
    :param config: the config dict
    """
    baseparms = _get_baseurlinfo_and_dist(ctx, remote, config)
    log.info("Installing packages: {pkglist} on remote rpm {arch}".format(
        pkglist=", ".join(rpm), arch=baseparms['arch']))
    host = ctx.teuthology_config.get('gitbuilder_host',
                                     'gitbuilder.ceph.com')
    dist_release = baseparms['dist_release']
    start_of_url = 'http://{host}/ceph-rpm-{distro_release}-{arch}-{flavor}/{uri}'.format(
        host=host, **baseparms)
    ceph_release = 'ceph-release-{release}.{dist_release}.noarch'.format(
        release=RELEASE, dist_release=dist_release)
    rpm_name = "{rpm_nm}.rpm".format(rpm_nm=ceph_release)
    base_url = "{start_of_url}/noarch/{rpm_name}".format(
        start_of_url=start_of_url, rpm_name=rpm_name)
    err_mess = StringIO()
    try:
        # When this was one command with a pipe, it would sometimes
        # fail with the message 'rpm: no packages given for install'
        remote.run(args=['wget', base_url, ],)
        remote.run(args=['sudo', 'rpm', '-i', rpm_name, ], stderr=err_mess, )
    except Exception:
        cmp_msg = 'package {pkg} is already installed'.format(
            pkg=ceph_release)
        if cmp_msg != err_mess.getvalue().strip():
            raise





def purge_data(ctx):
    """
    Purge /var/lib/ceph on every remote in ctx.

    :param ctx: the argparse.Namespace object
    """
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            p.spawn(_purge_data, remote)
            


def _purge_data(remote):
    """
    Purge /var/lib/ceph on remote.

    :param remote: the teuthology.orchestra.remote.Remote object
    """
    log.info('Purging /var/lib/ceph on %s', remote)
    remote.run(args=[
        'sudo',
        'rm', '-rf', '--one-file-system', '--', '/var/lib/ceph',
        run.Raw('||'),
        'true',
        run.Raw(';'),
        'test', '-d', '/var/lib/ceph',
        run.Raw('&&'),
        'sudo',
        'find', '/var/lib/ceph',
        '-mindepth', '1',
        '-maxdepth', '2',
        '-type', 'd',
        '-exec', 'umount', '{}', ';',
        run.Raw(';'),
        'sudo',
        'rm', '-rf', '--one-file-system', '--', '/var/lib/ceph',
    ])



def _remove_sources_list_rpm(remote, proj):
    """
    Removes /etc/yum.repos.d/{proj}.repo, /var/lib/{proj}, and /var/log/{proj}.

    :param remote: the teuthology.orchestra.remote.Remote object
    :param proj: the project whose sources.list needs removing
    """

    remote.run(
        args=[
            'sudo', 'zypper', '--non-interactive', 'rm', run.Raw('$('),
             'zypper', '--disable-system-resolvables', '-s',
             '0', 'packages', '-r', '{proj}'.format(proj=proj), run.Raw('|'), 'tail', 
             '-n', run.Raw('+4'), run.Raw('|'), 'cut', run.Raw("-d'|'"), 
             run.Raw('-f3'), run.Raw('|'), 'sort', '-u', run.Raw(')'),
             run.Raw('||'),
             'true',
        ],
        stdout=StringIO(),
    )

    if proj == 'ceph':
        remote.run(
        args=[
            'sudo', 'zypper', '--non-interactive', 'rm', run.Raw('$('),
             'zypper', '--disable-system-resolvables', '-s',
             '0', 'packages', '-r', 'ceph_extras', run.Raw('|'), 'tail',
             '-n', run.Raw('+4'), run.Raw('|'), 'cut', run.Raw("-d'|'"),
             run.Raw('-f3'), run.Raw('|'), 'sort', '-u', run.Raw(')'),
             run.Raw('||'),
             'true',
        ],
        stdout=StringIO(),
        )



    remote.run(
        args=[
            'sudo', 'rm', '-f', '/etc/zypp/repos.d/{proj}.repo'.format(
                proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )
    # FIXME
    # There probably should be a way of removing these files that is
    # implemented in the yum/rpm remove procedures for the ceph package.
    # FIXME but why is this function doing these things?
    remote.run(
        args=[
            'sudo', 'rm', '-fr', '/var/lib/{proj}'.format(proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )
    remote.run(
        args=[
            'sudo', 'rm', '-fr', '/var/log/{proj}'.format(proj=proj),
            run.Raw('||'),
            'true',
        ],
        stdout=StringIO(),
    )





def remove_sources(ctx, config):
    """
    Removes repo source files from each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    log.info("Removing {proj} sources lists".format(
        proj=config.get('project', 'ceph')))
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            system_type = teuthology.get_system_type(remote)
            p.spawn(_remove_sources_list_rpm,
                     remote, config.get('project', 'ceph'))
            p.spawn(_remove_sources_list_rpm,
                     remote, 'calamari')






def _remove_rpm(ctx, config, remote, rpm):
    """
    Removes RPM packages from remote

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    :param remote: the teuthology.orchestra.remote.Remote object
    :param rpm: list of packages names to remove
    """
    for pkg in rpm:
        pk_err_mess = StringIO()
        remote.run(args=['sudo', 'zypper', '--non-interactive', 
                    '--no-gpg-checks', '--quiet', 'remove', pkg, ],
                    stderr=pk_err_mess)
    
    
    
def remove_packages(ctx, config, pkgs):
    """
    Removes packages from each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    :param pkgs: list of packages names to remove
    """
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            system_type = teuthology.get_system_type(remote)
            p.spawn(_remove_rpm, 
                    ctx, config, remote, pkgs[system_type])
            
            
            

def _get_os_version(ctx, remote, config):
    os_alias = {'openSUSE project':'openSUSE_',
               'SUSE LINUX':'SLE_'}
    
    r = remote.run(
        args=['lsb_release', '-is'],
        stdout=StringIO(),
    )
    os_name = r.stdout.getvalue().strip()
    log.info("os name is %s" % (os_name))
    
    assert os_name in os_alias.keys(), \
        "unknown os found %s" %(os_name)
    
    version_alias = {'11':'11_SP3/', '12':'12/', '13.1':'13.1/'}
    r = remote.run(
        args=['lsb_release', '-rs'],
        stdout=StringIO(),
    )
    
    os_version = r.stdout.getvalue().strip()
    log.info("os version is %s" % (os_version))
    assert os_version in version_alias.keys(), \
        "unknown os found %s" %(os_version)
        
    retval = os_alias[os_name]+version_alias[os_version]
    log.info("os is %s" % (retval))
    return retval
    
    
def _add_repo(remote,baseurl,reponame):
    err_mess = StringIO()
    try:
        remote.run(args=['sudo', 'zypper', 'removerepo', reponame, ], stderr=err_mess, )
    except Exception:
        cmp_msg = 'Repository ceph not found by alias, number or URI.'
        if cmp_msg != err_mess.getvalue().strip():
            raise
        
    r = remote.run(
        args=['sudo', 'zypper', '--gpg-auto-import-keys',
               '--non-interactive', 'refresh'],
        stdout=StringIO(),
    )
        
    r = remote.run(
        args=['sudo', 'zypper', 'ar', baseurl, reponame],
        stdout=StringIO(),
    )
    
    r = remote.run(
        args=['sudo', 'zypper', '--gpg-auto-import-keys',
               '--non-interactive', 'refresh'],
        stdout=StringIO(),
    )


def _setRepoPriority(remote, reponame, pnum):
    err_mess = StringIO()
    r = remote.run(
        args=['sudo', 'zypper', 'mr', '-p',
               pnum, reponame],
        stdout=StringIO(),
    )
    r = remote.run(
        args=['sudo', 'zypper', 'ref',
               '-f'],
        stdout=StringIO(),
    )



def _downloadISOAddRepo(remote,baseurl,reponame,iso_name=None, is_internal=False):
    err_mess = StringIO()
    try:
        remote.run(args=['sudo', 'zypper', 'removerepo', reponame, ], stderr=err_mess, )
    except Exception:
        cmp_msg = 'Repository ceph not found by alias, number or URI.'
        if cmp_msg != err_mess.getvalue().strip():
            raise

    if iso_name == None:
        if is_internal==False:
            r = remote.run(
               args=['wget', '-q', '-O-', baseurl, run.Raw('|'), 'grep', run.Raw('Storage'), run.Raw('|'), 'grep', run.Raw('"DVD1\|Media1"'),
               run.Raw('|'), 'sed', '-e', run.Raw(' "s|.*SUSE-\(.*\)iso\(.*\)|\\1|" ')],
               stdout=StringIO(),
            )
        else:
           r = remote.run(
               args=['wget', '-q', '-O-', baseurl, run.Raw('|'), 'grep', run.Raw('Storage'), run.Raw('|'), 'grep', run.Raw('Internal'),
               run.Raw('|'), 'sed', '-e', run.Raw(' "s|.*SUSE-\(.*\)iso\(.*\)|\\1|" ')],
               stdout=StringIO(),
           )


        builds = r.stdout.getvalue().strip().split('\n')
        build_version = builds[len(builds)-1]
        iso_name = 'SUSE-'+build_version+'iso'

    log.info('ISO name  is - '+iso_name)
    iso_path = '/tmp/%s' % (iso_name)
    try:
         r = remote.run(
             args=['sudo', 'rm', iso_path],
             stdout=StringIO(),
         )
    except Exception:
         log.info('old ISO was not present in /tmp. nothing to delete.')
     
    iso_download_path = baseurl+'/'+iso_name
    r = remote.run(
         args=['wget', iso_download_path, '-P', '/tmp'],
         stdout=StringIO(),
    )

    iso_uri = 'iso:///?iso=%s'% (iso_path)
    log.info('ISO uri  is - '+iso_uri)

    r = remote.run(
        args=['sudo', 'zypper', 'ar', iso_uri, reponame],
        stdout=StringIO(),
    )

    r = remote.run(
        args=['sudo', 'zypper', '--gpg-auto-import-keys',
               '--non-interactive', 'refresh'],
        stdout=StringIO(),
    )








    
def _update_rpm_package_list_and_install(ctx, remote, rpm, config):
    baseparms = _get_os_version(ctx, remote, config)
    log.info("Installing packages: {pkglist} on remote os {os}".format(
        pkglist=", ".join(rpm), os=baseparms))
    host = ctx.teuthology_config.get('gitbuilder_host',
            'download.suse.de/ibs/Devel:/Storage:/0.5:/Staging/')
    baseurl = host+baseparms
    baseurl = host #temporary fix, will be changed soon
    baseurl_extra = ctx.teuthology_config.get('gitbuilder_host_extra',
            'http://download.suse.de/ibs/Devel:/Storage:/1.0/SLE_12/')
    _downloadISOAddRepo(remote,baseurl,'ceph')
    _downloadISOAddRepo(remote,baseurl_extra,'ceph_extras',iso_name=None, is_internal=True)
    #_add_repo(remote,baseurl_extra,'ceph_extras')
    _setRepoPriority(remote, 'ceph_extras', '100')
    
    for pkg in rpm:
        pk_err_mess = StringIO()
        remote.run(args=['sudo', 'zypper', '--non-interactive', 
                    '--no-gpg-checks', '--quiet', 'in',pkg, ],
                    stderr=pk_err_mess)
    for pkg in rpm_extras_packages:
        pk_err_mess = StringIO()
        remote.run(args=['sudo', 'zypper', '--non-interactive',
                    '--no-gpg-checks', '--quiet', 'in', '-r', 'ceph_extras',pkg, ],
                    stderr=pk_err_mess) 
    
    
    
def install_packages(ctx, pkgs, config):
    """
    Installs packages on each remote in ctx.

    :param ctx: the argparse.Namespace object
    :param pkgs: list of packages names to install
    :param config: the config dict
    """
    with parallel() as p:
        for remote in ctx.cluster.remotes.iterkeys():
            p.spawn(
                _update_rpm_package_list_and_install,
                ctx, remote, pkgs['rpm'], config) 


@contextlib.contextmanager
def install(ctx, config):
    """
    The install task. Installs packages for a given project on all hosts in
    ctx. May work for projects besides ceph, but may not. Patches welcomed!

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """

    project = config.get('project', 'ceph')

    global rpm_packages
    rpm = rpm_packages.get(project, [])

    # pull any additional packages out of config
    extra_pkgs = config.get('extra_packages')
    log.info('extra packages: {packages}'.format(packages=extra_pkgs))
    rpm += extra_pkgs

    # the extras option right now is specific to the 'ceph' project
    extras = config.get('extras')
    if extras is not None:
        rpm = ['ceph-fuse', 'librbd1', 'librados2', 'ceph-test', 'python-ceph']

    # install lib deps (so we explicitly specify version), but do not
    # uninstall them, as other packages depend on them (e.g., kvm)
    proj_install_debs = {'ceph': [
        'librados2',
        'librados2-dbg',
        'librbd1',
        'librbd1-dbg',
    ]}

    proj_install_rpm = {'ceph': [
        'librbd1',
        'librados2',
    ]}

    install_debs = proj_install_debs.get(project, [])
    install_rpm = proj_install_rpm.get(project, [])

    install_info = {
        "rpm": rpm + install_rpm}
    remove_info = {
        "rpm": rpm}
    install_packages(ctx, install_info, config)
    try:
        yield
    finally:
        remove_packages(ctx, config, remove_info)
        remove_sources(ctx, config)
        if project == 'ceph':
            purge_data(ctx)




def upgrade_old_style(ctx, node, remote, pkgs, system_type):
    """
    Handle the upgrade using methods in use prior to ceph-deploy.
    """
    if system_type == 'deb':
        _upgrade_deb_packages(ctx, node, remote, pkgs)
    elif system_type == 'rpm':
        _upgrade_rpm_packages(ctx, node, remote, pkgs)

def upgrade_with_ceph_deploy(ctx, node, remote, pkgs, sys_type):
    """
    Upgrade using ceph-deploy
    """
    dev_table = ['branch', 'tag', 'dev']
    ceph_dev_parm = ''
    ceph_rel_parm = ''
    for entry in node.keys():
        if entry in dev_table:
            ceph_dev_parm = node[entry]
        if entry == 'release':
            ceph_rel_parm = node[entry]
    params = []
    if ceph_dev_parm:
        params += ['--dev', ceph_dev_parm]
    if ceph_rel_parm:
        params += ['--release', ceph_rel_parm]
    params.append(remote.name)
    subprocess.call(['ceph-deploy', 'install'] + params)
    remote.run(args=['sudo', 'restart', 'ceph-all'])

def upgrade_common(ctx, config, deploy_style):
    """
    Common code for upgrading
    """

    assert config is None or isinstance(config, dict), \
        "install.upgrade only supports a dictionary for configuration"

    for i in config.keys():
            assert config.get(i) is None or isinstance(
                config.get(i), dict), 'host supports dictionary'

    project = config.get('project', 'ceph')

    # use 'install' overrides here, in case the upgrade target is left
    # unspecified/implicit.
    install_overrides = ctx.config.get(
        'overrides', {}).get('install', {}).get(project, {})
    log.info('project %s config %s overrides %s', project, config, install_overrides)

    # FIXME: extra_pkgs is not distro-agnostic
    extra_pkgs = config.get('extra_packages', [])
    log.info('extra packages: {packages}'.format(packages=extra_pkgs))

    # build a normalized remote -> config dict
    remotes = {}
    if 'all' in config:
        for remote in ctx.cluster.remotes.iterkeys():
            remotes[remote] = config.get('all')
    else:
        for role in config.keys():
            (remote,) = ctx.cluster.only(role).remotes.iterkeys()
            if remote in remotes:
                log.warn('remote %s came up twice (role %s)', remote, role)
                continue
            remotes[remote] = config.get(role)

    for remote, node in remotes.iteritems():
        if not node:
            node = {}

        this_overrides = copy.deepcopy(install_overrides)
        if 'sha1' in node or 'tag' in node or 'branch' in node:
            log.info('config contains sha1|tag|branch, removing those keys from override')
            this_overrides.pop('sha1', None)
            this_overrides.pop('tag', None)
            this_overrides.pop('branch', None)
        teuthology.deep_merge(node, this_overrides)
        log.info('remote %s config %s', remote, node)

        system_type = teuthology.get_system_type(remote)
        assert system_type in ('deb', 'rpm')
        pkgs = PACKAGES[project][system_type]
        log.info("Upgrading {proj} {system_type} packages: {pkgs}".format(
            proj=project, system_type=system_type, pkgs=', '.join(pkgs)))
            # FIXME: again, make extra_pkgs distro-agnostic
        pkgs += extra_pkgs
        node['project'] = project
        
        deploy_style(ctx, node, remote, pkgs, system_type)


docstring_for_upgrade = """"
    Upgrades packages for a given project.

    For example::

        tasks:
        - install.{cmd_parameter}:
             all:
                branch: end

    or specify specific roles::

        tasks:
        - install.{cmd_parameter}:
             mon.a:
                branch: end
             osd.0:
                branch: other

    or rely on the overrides for the target version::

        overrides:
          install:
            ceph:
              sha1: ...
        tasks:
        - install.{cmd_parameter}:
            all:

    (HACK: the overrides will *only* apply the sha1/branch/tag if those
    keys are not present in the config.)

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """

#
# __doc__ strings for upgrade and ceph_deploy_upgrade are set from
# the same string so that help(upgrade) and help(ceph_deploy_upgrade)
# look the same.
#

@contextlib.contextmanager
def upgrade(ctx, config):
    upgrade_common(ctx, config, upgrade_old_style)
    yield

upgrade.__doc__ = docstring_for_upgrade.format(cmd_parameter='upgrade')

@contextlib.contextmanager
def ceph_deploy_upgrade(ctx, config):
    upgrade_common(ctx, config, upgrade_with_ceph_deploy)
    yield

ceph_deploy_upgrade.__doc__ = docstring_for_upgrade.format(
        cmd_parameter='ceph_deploy_upgrade')

@contextlib.contextmanager
def ship_utilities(ctx, config):
    """
    Write a copy of valgrind.supp to each of the remote sites.  Set executables used
    by Ceph in /usr/local/bin.  When finished (upon exit of the teuthology run), remove
    these files.

    :param ctx: Context
    :param config: Configuration
    """
    assert config is None
    testdir = teuthology.get_testdir(ctx)
    filenames = []

    log.info('Shipping valgrind.supp...')
    with file(os.path.join(os.path.dirname(__file__), 'valgrind.supp'), 'rb') as f:
        fn = os.path.join(testdir, 'valgrind.supp')
        filenames.append(fn)
        for rem in ctx.cluster.remotes.iterkeys():
            teuthology.sudo_write_file(
                remote=rem,
                path=fn,
                data=f,
                )
            f.seek(0)

    FILES = ['daemon-helper', 'adjust-ulimits']
    destdir = '/usr/bin'
    for filename in FILES:
        log.info('Shipping %r...', filename)
        src = os.path.join(os.path.dirname(__file__), filename)
        dst = os.path.join(destdir, filename)
        filenames.append(dst)
        with file(src, 'rb') as f:
            for rem in ctx.cluster.remotes.iterkeys():
                teuthology.sudo_write_file(
                    remote=rem,
                    path=dst,
                    data=f,
                )
                f.seek(0)
                rem.run(
                    args=[
                        'sudo',
                        'chmod',
                        'a=rx',
                        '--',
                        dst,
                    ],
                )

    try:
        yield
    finally:
        log.info('Removing shipped files: %s...', ' '.join(filenames))
        run.wait(
            ctx.cluster.run(
                args=[
                    'sudo',
                    'rm',
                    '-f',
                    '--',
                ] + list(filenames),
                wait=False,
            ),
        )



@contextlib.contextmanager
def task(ctx, config):
    """
    Install packages for a given project.

    tasks:
    - install:
        project: ceph
        branch: bar
    - install:
        project: samba
        branch: foo
        extra_packages: ['samba']

    Overrides are project specific:

    overrides:
      install:
        ceph:
          sha1: ...

    :param ctx: the argparse.Namespace object
    :param config: the config dict
    """
    if config is None:
        config = {}
    assert isinstance(config, dict), \
        "task install only supports a dictionary for configuration"

    project, = config.get('project', 'ceph'),
    log.debug('project %s' % project)
    overrides = ctx.config.get('overrides')
    if overrides:
        install_overrides = overrides.get('install', {})
        teuthology.deep_merge(config, install_overrides.get(project, {}))
    log.debug('config %s' % config)

    # Flavor tells us what gitbuilder to fetch the prebuilt software
    # from. It's a combination of possible keywords, in a specific
    # order, joined by dashes. It is used as a URL path name. If a
    # match is not found, the teuthology run fails. This is ugly,
    # and should be cleaned up at some point.

    flavor = config.get('flavor', 'basic')

    if config.get('path'):
        # local dir precludes any other flavors
        flavor = 'local'
        log.debug('config:path ' + config.get('path'))
    else:
        if config.get('valgrind'):
            log.info(
                'Using notcmalloc flavor and running some daemons under valgrind')
            flavor = 'notcmalloc'
        else:
            if config.get('coverage'):
                log.info('Recording coverage for this run.')
                flavor = 'gcov'

    ctx.summary['flavor'] = flavor

    with contextutil.nested(
        lambda: install(ctx=ctx, config=dict(
            branch=config.get('branch'),
            tag=config.get('tag'),
            sha1=config.get('sha1'),
            flavor=flavor,
            extra_packages=config.get('extra_packages', []),
            extras=config.get('extras', None),
            wait_for_package=ctx.config.get('wait_for_package', False),
            project=project,
        )),
        lambda: ship_utilities(ctx=ctx, config=None),
    ):
        yield
