# -*- test-case-name: admin.test.test_release -*-
# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Helper utilities for the Flocker release process.

XXX This script is not automatically checked by buildbot. See
https://clusterhq.atlassian.net/browse/FLOC-397
"""

import os
import sys
import tempfile

from setuptools import __version__ as setuptools_version
from subprocess import check_call

from effect import (
    Effect, sync_perform, ComposedDispatcher)
from effect.do import do

from characteristic import attributes
from git import GitCommandError, Repo

from twisted.python.filepath import FilePath
from twisted.python.usage import Options, UsageError
from twisted.python.constants import Names, NamedConstant
from twisted.web import template

import flocker
from flocker.provision._effect import sequence, dispatcher as base_dispatcher

from flocker.common.version import (
    get_doc_version,
    get_pre_release,
    is_pre_release,
    is_release,
    is_weekly_release,
    target_release,
    UnparseableVersion,
)
from flocker.provision._install import ARCHIVE_BUCKET

from .aws import (
    boto_dispatcher,
    UpdateS3RoutingRule,
    UpdateS3ErrorPage,
    ListS3Keys,
    DeleteS3Keys,
    CopyS3Keys,
    DownloadS3KeyRecursively,
    UploadToS3,
    UploadToS3Recursively,
    CreateCloudFrontInvalidation,

)

from .yum import (
    yum_dispatcher,
    CreateRepo,
    DownloadPackagesFromRepository,
)

from .homebrew import make_recipe
from .packaging import Distribution


class NotTagged(Exception):
    """
    Raised if publishing to production and the version being published version
    isn't tagged.
    """


class NotARelease(Exception):
    """
    Raised if trying to publish documentation to, or packages for a version
    that isn't a release.
    """


class DocumentationRelease(Exception):
    """
    Raised if trying to upload packages for a documentation release.
    """


class Environments(Names):
    """
    The environments that documentation can be published to.
    """
    PRODUCTION = NamedConstant()
    STAGING = NamedConstant()


class TagExists(Exception):
    """
    Raised if trying to release a version for which a tag already exists.
    """


class BranchExists(Exception):
    """
    Raised if trying to release a version for which a branch already exists.
    """


class BaseBranchDoesNotExist(Exception):
    """
    Raised if trying to release a version for which the expected base branch
    does not exist.
    """


class MissingPreRelease(Exception):
    """
    Raised if trying to release a pre-release for which the previous expected
    pre-release does not exist.
    """


class NoPreRelease(Exception):
    """
    Raised if trying to release a marketing release if no pre-release exists.
    """


class PushFailed(Exception):
    """
    Raised if pushing to Git fails.
    """


class IncorrectSetuptoolsVersion(Exception):
    """
    Raised if trying to create packages which require a specific version of
    setuptools to be installed.
    """


@attributes([
    'documentation_bucket',
    'cloudfront_cname',
    'dev_bucket',
])
class DocumentationConfiguration(object):
    """
    The configuration for publishing documentation.

    :ivar bytes documentation_bucket: The bucket to publish documentation to.
    :ivar bytes cloudfront_cname: a CNAME associated to the cloudfront
        distribution pointing at the documentation bucket.
    :ivar bytes dev_bucket: The bucket buildbot uploads documentation to.
    """

DOCUMENTATION_CONFIGURATIONS = {
    Environments.PRODUCTION:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-docs",
            cloudfront_cname="docs.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
    Environments.STAGING:
        DocumentationConfiguration(
            documentation_bucket="clusterhq-staging-docs",
            cloudfront_cname="docs.staging.clusterhq.com",
            dev_bucket="clusterhq-dev-docs"),
}


@do
def publish_docs(flocker_version, doc_version, environment):
    """
    Publish the Flocker documentation. The documentation for each version of
    Flocker is uploaded to a development bucket on S3 by the build server and
    this copies the documentation for a particular ``flocker_version`` and
    publishes it as ``doc_version``. Attempting to publish documentation to a
    staging environment as a documentation version publishes it as the version
    being updated.

    :param bytes flocker_version: The version of Flocker to publish the
        documentation for.
    :param bytes doc_version: The version to publish the documentation as.
    :param Environments environment: The environment to publish the
        documentation to.
    :raises NotARelease: Raised if trying to publish to a version that isn't a
        release.
    :raises NotTagged: Raised if publishing to production and the version being
        published version isn't tagged.
    """
    if not (is_release(doc_version)
            or is_weekly_release(doc_version)
            or is_pre_release(doc_version)):
        raise NotARelease()

    if environment == Environments.PRODUCTION:
        if get_doc_version(flocker_version) != doc_version:
            raise NotTagged()
    configuration = DOCUMENTATION_CONFIGURATIONS[environment]

    dev_prefix = '%s/' % (flocker_version,)
    version_prefix = 'en/%s/' % (get_doc_version(doc_version),)

    is_dev = not is_release(doc_version)
    if is_dev:
        stable_prefix = "en/devel/"
    else:
        stable_prefix = "en/latest/"

    # Get the list of keys in the new documentation.
    new_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.dev_bucket,
                   prefix=dev_prefix))
    # Get the list of keys already existing for the given version.
    # This should only be non-empty for documentation releases.
    existing_version_keys = yield Effect(
        ListS3Keys(bucket=configuration.documentation_bucket,
                   prefix=version_prefix))

    # Copy the new documentation to the documentation bucket.
    yield Effect(
        CopyS3Keys(source_bucket=configuration.dev_bucket,
                   source_prefix=dev_prefix,
                   destination_bucket=configuration.documentation_bucket,
                   destination_prefix=version_prefix,
                   keys=new_version_keys))

    # Delete any keys that aren't in the new documentation.
    yield Effect(
        DeleteS3Keys(bucket=configuration.documentation_bucket,
                     prefix=version_prefix,
                     keys=existing_version_keys - new_version_keys))

    # Update the key used for error pages if we're publishing to staging or if
    # we're publishing a marketing release to production.
    if ((environment is Environments.STAGING) or
        (environment is Environments.PRODUCTION and not is_dev)):
        yield Effect(
            UpdateS3ErrorPage(bucket=configuration.documentation_bucket,
                              target_prefix=version_prefix))

    # Update the redirect for the stable URL (en/latest/ or en/devel/)
    # to point to the new version. Returns the old target.
    old_prefix = yield Effect(
        UpdateS3RoutingRule(bucket=configuration.documentation_bucket,
                            prefix=stable_prefix,
                            target_prefix=version_prefix))

    # If we have changed versions, get all the keys from the old version
    if old_prefix:
        previous_version_keys = yield Effect(
            ListS3Keys(bucket=configuration.documentation_bucket,
                       prefix=old_prefix))
    else:
        previous_version_keys = set()

    # The changed keys are the new keys, the keys that were deleted from this
    # version, and the keys for the previous version.
    changed_keys = (new_version_keys |
                    existing_version_keys |
                    previous_version_keys)

    # S3 serves /index.html when given /, so any changed /index.html means
    # that / changed as well.
    # Note that we check for '/index.html' but remove 'index.html'
    changed_keys |= {key_name[:-len('index.html')]
                     for key_name in changed_keys
                     if key_name.endswith('/index.html')}

    # Always update the root.
    changed_keys |= {''}

    # The full paths are all the changed keys under the stable prefix, and
    # the new version prefix. This set is slightly bigger than necessary.
    changed_paths = {prefix + key_name
                     for key_name in changed_keys
                     for prefix in [stable_prefix, version_prefix]}

    # Invalidate all the changed paths in cloudfront.
    yield Effect(
        CreateCloudFrontInvalidation(cname=configuration.cloudfront_cname,
                                     paths=changed_paths))


class PublishDocsOptions(Options):
    """
    Arguments for ``publish-docs`` script.
    """

    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of flocker from which the documentation was built."],
        ["doc-version", None, None,
         "The version to publish the documentation as.\n"
         "This will differ from \"flocker-version\" for staging uploads."
         "Attempting to publish documentation as a documentation version "
         "publishes it as the version being updated.\n"
         "``doc-version`` is set to 0.3.0+doc1 the documentation will be "
         "published as 0.3.0.\n"],
    ]

    optFlags = [
        ["production", None, "Publish documentation to production."],
    ]

    environment = Environments.STAGING

    def parseArgs(self):
        if self['doc-version'] is None:
            self['doc-version'] = get_doc_version(self['flocker-version'])

        if self['production']:
            self.environment = Environments.PRODUCTION


def publish_docs_main(args, base_path, top_level):
    """
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = PublishDocsOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    try:
        sync_perform(
            dispatcher=ComposedDispatcher([boto_dispatcher, base_dispatcher]),
            effect=publish_docs(
                flocker_version=options['flocker-version'],
                doc_version=options['doc-version'],
                environment=options.environment,
                ))
    except NotARelease:
        sys.stderr.write("%s: Can't publish non-release.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except NotTagged:
        sys.stderr.write(
            "%s: Can't publish non-tagged version to production.\n"
            % (base_path.basename(),))
        raise SystemExit(1)


class UploadOptions(Options):
    """
    Options for uploading artifacts.
    """
    optParameters = [
        ["flocker-version", None, flocker.__version__,
         "The version of Flocker to upload packages for."
         "Python packages for " + flocker.__version__ + "will be uploaded.\n"],
        ["target", None, ARCHIVE_BUCKET,
         "The bucket to upload artifacts to.\n"],
        ["build-server", None,
         b'http://build.clusterhq.com',
         "The URL of the build-server.\n"],
        ["homebrew-tap", None, "git@github.com:ClusterHQ/homebrew-tap.git",
         "The Git repository to add a Homebrew recipe to.\n"],
    ]

    def parseArgs(self):
        version = self['flocker-version']

        if not (is_release(version)
                or is_weekly_release(version)
                or is_pre_release(version)):
            raise NotARelease()

        if get_doc_version(version) != version:
            raise DocumentationRelease()


FLOCKER_PACKAGES = [
    b'clusterhq-python-flocker',
    b'clusterhq-flocker-cli',
    b'clusterhq-flocker-node',
]


def publish_homebrew_recipe(homebrew_repo_url, version, source_bucket,
                            scratch_directory):
    """
    Publish a Homebrew recipe to a Git repository.

    :param git.Repo homebrew_repo: Homebrew tap Git repository. This should
        be an SSH URL so as not to require a username and password.
    :param bytes version: Version of Flocker to publish a recipe for.
    :param bytes source_bucket: S3 bucket to get source distribution from.
    :param FilePath scratch_directory: Temporary directory to create a recipe
        in.
    """
    url_template = 'https://{bucket}.s3.amazonaws.com/python/Flocker-{version}.tar.gz'  # noqa
    sdist_url = url_template.format(bucket=source_bucket, version=version)
    content = make_recipe(
        version=version,
        sdist_url=sdist_url)
    homebrew_repo = Repo.clone_from(
        url=homebrew_repo_url,
        to_path=scratch_directory.path)
    recipe = 'flocker-{version}.rb'.format(version=version)
    FilePath(homebrew_repo.working_dir).child(recipe).setContent(content)

    homebrew_repo.index.add([recipe])
    homebrew_repo.index.commit('Add recipe for Flocker version ' + version)
    push_info = homebrew_repo.remotes.origin.push(homebrew_repo.head)[0]
    if (push_info.flags & push_info.ERROR) != 0:
        raise PushFailed()


@do
def update_repo(package_directory, target_bucket, target_key, source_repo,
                packages, flocker_version, distro_name, distro_version):
    """
    Update ``target_bucket`` yum repository with ``packages`` from
    ``source_repo`` repository.

    :param FilePath package_directory: Temporary directory to download
        repository to.
    :param bytes target_bucket: S3 bucket to upload repository to.
    :param bytes target_key: Path within S3 bucket to upload repository to.
    :param bytes source_repo: Repository on the build server to get packages
        from.
    :param list packages: List of bytes, each specifying the name of a package
        to upload to the repository.
    :param bytes flocker_version: The version of flocker to upload packages
        for.
    :param distro_name: The name of the distribution to upload packages for.
    :param distro_version: The distro_version of the distribution to upload
        packages for.
    """
    package_directory.createDirectory()

    distribution = Distribution(
        name=distro_name,
        version=distro_version,
    )
    package_type = distribution.package_type()

    yield Effect(DownloadS3KeyRecursively(
        source_bucket=target_bucket,
        source_prefix=target_key,
        target_path=package_directory,
        filter_extensions=('.' + package_type.value,)))

    downloaded_packages = yield Effect(DownloadPackagesFromRepository(
        source_repo=source_repo,
        target_path=package_directory,
        packages=packages,
        flocker_version=flocker_version,
        distro_name=distro_name,
        distro_version=distro_version,
        ))

    new_metadata = yield Effect(CreateRepo(
        repository_path=package_directory,
        distro_name=distro_name,
        distro_version=distro_version,
        ))

    yield Effect(UploadToS3Recursively(
        source_path=package_directory,
        target_bucket=target_bucket,
        target_key=target_key,
        files=downloaded_packages | new_metadata,
        ))


@do
def copy_tutorial_vagrant_box(target_bucket, dev_bucket, version):
    """
    Copy the tutorial box from a ``dev_bucket`` to a ``target_bucket``.

    :param bytes target_bucket: S3 bucket to copy tutorial box to.
    :param bytes dev_bucket: S3 bucket to copy tutorial box from.
    :param bytes version: Version of Flocker to copy the tutorial box for.
    """
    yield Effect(
        CopyS3Keys(source_bucket=dev_bucket,
                   source_prefix='vagrant/tutorial/',
                   destination_bucket=target_bucket,
                   destination_prefix='vagrant/tutorial/',
                   keys=['flocker-tutorial-{}.box'.format(version)]))



@do
def upload_rpms(scratch_directory, target_bucket, version, build_server):
    """
    The ClusterHQ yum repository contains packages for Flocker, as well as the
    dependencies which aren't available in Fedora 20 or CentOS 7. It is
    currently hosted on Amazon S3. When doing a release, we want to add the
    new Flocker packages, while preserving the existing packages in the
    repository. To do this, we download the current repository, add the new
    package, update the metadata, and then upload the repository.

    :param FilePath scratch_directory: Temporary directory to download
        repository to.
    :param bytes target_bucket: S3 bucket to upload repository to.
    :param bytes version: Version to download RPMs for.
    :param bytes build_server: Server to download new RPMs from.
    """
    is_dev = not is_release(version)
    if is_dev:
        target_distro_suffix = "-testing"
    else:
        target_distro_suffix = ""

    operating_systems = [
        {'distro': 'fedora', 'version': '20', 'arch': 'x86_64'},
        {'distro': 'centos', 'version': '7', 'arch': 'x86_64'},
        {'distro': 'ubuntu', 'version': '14.04', 'arch': 'amd64'},
    ]

    for operating_system in operating_systems:
        yield update_repo(
            package_directory=scratch_directory.child(
                b'{}-{}-{}'.format(
                    operating_system['distro'],
                    operating_system['version'],
                    operating_system['arch'])),
            target_bucket=target_bucket,
            target_key=os.path.join(
                operating_system['distro'] + target_distro_suffix,
                operating_system['version'],
                operating_system['arch']),
            source_repo=os.path.join(
                build_server, b'results/omnibus',
                version,
                b'{}-{}'.format(
                    operating_system['distro'],
                    operating_system['version'])),
            packages=FLOCKER_PACKAGES,
            flocker_version=version,
            distro_name=operating_system['distro'],
            distro_version=operating_system['version'],
        )


packages_template = (
    '<html xmlns:t="http://twistedmatrix.com/ns/twisted.web.template/0.1">\n'
    'This is an index for pip\n'
    '<div t:render="packages"><a>'
    '<t:attr name="href"><t:slot name="package_name" /></t:attr>'
    '<t:slot name="package_name" />'
    '</a><br />\n</div>'
    '</html>'
    )


class PackagesElement(template.Element):
    """A Twisted Web template element to render the Pip index file."""

    def __init__(self, packages):
        template.Element.__init__(self, template.XMLString(packages_template))
        self._packages = packages

    @template.renderer
    def packages(self, request, tag):
        for package in self._packages:
            if package != 'index.html':
                yield tag.clone().fillSlots(package_name=package)


def create_pip_index(scratch_directory, packages):
    """
    Create an index file for pip.

    :param FilePath scratch_directory: Temporary directory to create index in.
    :param list packages: List of bytes, filenames of packages to be in the
        index.
    """
    index_file = scratch_directory.child('index.html')
    with index_file.open('w') as f:
        # Although this returns a Deferred, it works without the reactor
        # because there are no Deferreds in the template evaluation.
        # See this cheat described at
        # https://twistedmatrix.com/documents/15.0.0/web/howto/twisted-templates.html
        template.flatten(None, PackagesElement(packages), f.write)
    return index_file


@do
def upload_pip_index(scratch_directory, target_bucket):
    """
    Upload an index file for pip to S3.

    :param FilePath scratch_directory: Temporary directory to create index in.
    :param bytes target_bucket: S3 bucket to upload index to.
    """
    packages = yield Effect(
        ListS3Keys(bucket=target_bucket,
                   prefix='python/'))

    index_path = create_pip_index(
        scratch_directory=scratch_directory,
        packages=packages)

    yield Effect(
        UploadToS3(
            source_path=scratch_directory,
            target_bucket=target_bucket,
            target_key='python/index.html',
            file=index_path,
        ))


@do
def upload_python_packages(scratch_directory, target_bucket, top_level,
                           output, error):
    """
    The repository contains source distributions and binary distributions
    (wheels) for Flocker. It is currently hosted on Amazon S3.

    :param FilePath scratch_directory: Temporary directory to create packages
        in.
    :param bytes target_bucket: S3 bucket to upload packages to.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    if setuptools_version != '3.6':
        # XXX Use PEP440 version system so new setuptools can be used.
        # https://clusterhq.atlassian.net/browse/FLOC-1331.
        raise IncorrectSetuptoolsVersion()

    # XXX This has a side effect so it should be an Effect
    # https://clusterhq.atlassian.net/browse/FLOC-1731
    check_call([
        'python', 'setup.py',
        'sdist', '--dist-dir={}'.format(scratch_directory.path),
        'bdist_wheel', '--dist-dir={}'.format(scratch_directory.path)],
        cwd=top_level.path, stdout=output, stderr=error)

    files = set([file.basename() for file in scratch_directory.children()])
    yield Effect(UploadToS3Recursively(
        source_path=scratch_directory,
        target_bucket=target_bucket,
        target_key='python',
        files=files,
        ))


def publish_artifacts_main(args, base_path, top_level):
    """
    Publish release artifacts.

    :param list args: The arguments passed to the scripts.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = UploadOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)
    except NotARelease:
        sys.stderr.write("%s: Can't publish artifacts for a non-release.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except DocumentationRelease:
        sys.stderr.write("%s: Can't publish artifacts for a documentation "
                         "release.\n" % (base_path.basename(),))
        raise SystemExit(1)

    dispatcher = ComposedDispatcher([boto_dispatcher, yum_dispatcher,
                                     base_dispatcher])

    scratch_directory = FilePath(tempfile.mkdtemp(
        prefix=b'flocker-upload-'))
    scratch_directory.child('rpm').createDirectory()
    scratch_directory.child('python').createDirectory()
    scratch_directory.child('pip').createDirectory()
    scratch_directory.child('homebrew').createDirectory()

    try:
        sync_perform(
            dispatcher=dispatcher,
            effect=sequence([
                upload_rpms(
                    scratch_directory=scratch_directory.child('rpm'),
                    target_bucket=options['target'],
                    version=options['flocker-version'],
                    build_server=options['build-server'],
                ),
                upload_python_packages(
                    scratch_directory=scratch_directory.child('python'),
                    target_bucket=options['target'],
                    top_level=top_level,
                    output=sys.stdout,
                    error=sys.stderr,
                ),
                upload_pip_index(
                    scratch_directory=scratch_directory.child('pip'),
                    target_bucket=options['target'],
                ),
                copy_tutorial_vagrant_box(
                    target_bucket=options['target'],
                    dev_bucket='clusterhq-dev-archive',
                    version=options['flocker-version'],
                )
            ]),
        )

        publish_homebrew_recipe(
            homebrew_repo_url=options['homebrew-tap'],
            version=options['flocker-version'],
            source_bucket=options['target'],
            scratch_directory=scratch_directory.child('homebrew'),
        )

    except IncorrectSetuptoolsVersion:
        sys.stderr.write("%s: setuptools version must be 3.6.\n"
            % (base_path.basename(),))
        raise SystemExit(1)
    finally:
        scratch_directory.remove()


def calculate_base_branch(version, path):
    """
    The branch a release branch is created from depends on the release
    type and sometimes which pre-releases have preceeded this.

    :param bytes version: The version of Flocker to get a base branch for.
    :param bytes path: See :func:`git.Repo.init`.
    :returns: The base branch from which the new release branch was created.
    """
    if not (is_release(version)
            or is_weekly_release(version)
            or is_pre_release(version)):
        raise NotARelease()

    repo = Repo(path=path, search_parent_directories=True)
    existing_tags = [tag for tag in repo.tags if tag.name == version]

    if existing_tags:
        raise TagExists()

    release_branch_prefix = 'release/flocker-'

    if is_weekly_release(version):
        base_branch_name = 'master'
    elif is_pre_release(version) and get_pre_release(version) == 1:
        base_branch_name = 'master'
    elif get_doc_version(version) != version:
        base_branch_name = release_branch_prefix + get_doc_version(version)
    else:
        if is_pre_release(version):
            target_version = target_release(version)
        else:
            target_version = version

        pre_releases = []
        for tag in repo.tags:
            try:
                if (is_pre_release(tag.name) and
                    target_version == target_release(tag.name)):
                    pre_releases.append(tag.name)
            except UnparseableVersion:
                # The Flocker repository contains versions which are not
                # currently considered valid versions.
                pass

        if not pre_releases:
            raise NoPreRelease()

        latest_pre_release = sorted(
            pre_releases,
            key=lambda pre_release: get_pre_release(pre_release))[-1]

        if (is_pre_release(version) and get_pre_release(version) >
                get_pre_release(latest_pre_release) + 1):
            raise MissingPreRelease()

        base_branch_name = release_branch_prefix + latest_pre_release

    # We create a new branch from a branch, not a tag, because a maintenance
    # or documentation change may have been applied to the branch and not the
    # tag.
    try:
        base_branch = [
            branch for branch in repo.branches if
            branch.name == base_branch_name][0]
    except IndexError:
        raise BaseBranchDoesNotExist()

    return base_branch


def create_release_branch(version, base_branch):
    """
    checkout a new Git branch to make changes on and later tag as a release.

    :param bytes version: The version of Flocker to create a release branch
        for.
    :param base_branch: See :func:`git.Head`. The branch to create the release
        branch from.
    """
    try:
        base_branch.checkout(b='release/flocker-' + version)
    except GitCommandError:
        raise BranchExists()


class CreateReleaseBranchOptions(Options):
    """
    Arguments for ``create-release-branch`` script.
    """

    optParameters = [
        ["flocker-version", None, None,
         "The version of Flocker to create a release branch for."],
    ]

    def parseArgs(self):
        if self['flocker-version'] is None:
            raise UsageError("`--flocker-version` must be specified.")


def create_release_branch_main(args, base_path, top_level):
    """
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the flocker repository.
    """
    options = CreateReleaseBranchOptions()

    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    version = options['flocker-version']
    path = FilePath(__file__).path

    try:
        base_branch = calculate_base_branch(version=version, path=path)
        create_release_branch(version=version, base_branch=base_branch)
    except NotARelease:
        sys.stderr.write("%s: Can't create a release branch for non-release.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except TagExists:
        sys.stderr.write("%s: Tag already exists for this release.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except NoPreRelease:
        sys.stderr.write("%s: No (previous) pre-release exists for this "
                         "release.\n" % (base_path.basename(),))
        raise SystemExit(1)
    except BaseBranchDoesNotExist:
        sys.stderr.write("%s: The expected base branch does not exist.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
    except BranchExists:
        sys.stderr.write("%s: The release branch already exists.\n"
                         % (base_path.basename(),))
        raise SystemExit(1)
