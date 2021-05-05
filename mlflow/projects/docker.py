import logging
import os
import posixpath
import shutil
import tempfile
import urllib.parse
import urllib.request

import docker

from mlflow import tracking
from mlflow.projects.utils import get_databricks_env_vars
from mlflow.exceptions import ExecutionException
from mlflow.projects.utils import MLFLOW_DOCKER_WORKDIR_PATH
from mlflow.tracking.context.git_context import _get_git_commit
from mlflow.utils import process, file_utils
from mlflow.utils.mlflow_tags import MLFLOW_DOCKER_IMAGE_URI, MLFLOW_DOCKER_IMAGE_ID

_logger = logging.getLogger(__name__)

_GENERATED_DOCKERFILE_NAME = "Dockerfile.mlflow-autogenerated"
_MLFLOW_DOCKER_TRACKING_DIR_PATH = "/mlflow/tmp/mlruns"
_PROJECT_TAR_ARCHIVE_NAME = "mlflow-project-docker-build-context"


def validate_docker_installation():
    """
    Verify if Docker is installed on host machine.
    """
    try:
        docker_path = "docker"
        process.exec_cmd([docker_path, "--help"], throw_on_error=False)
    except EnvironmentError:
        raise ExecutionException(
            "Could not find Docker executable. "
            "Ensure Docker is installed as per the instructions "
            "at https://docs.docker.com/install/overview/."
        )


def validate_docker_env(project):
    if not project.name:
        raise ExecutionException(
            "Project name in MLProject must be specified when using docker " "for image tagging."
        )
    if not project.docker_env.get("image"):
        raise ExecutionException(
            "Project with docker environment must specify the docker image "
            "to use via an 'image' field under the 'docker_env' field."
        )


def build_docker_image(work_dir, repository_uri, base_image, run_id):
    """
    Build a docker image containing the project in `work_dir`, using the base image.
    """
    image_uri = _get_docker_image_uri(repository_uri=repository_uri, work_dir=work_dir)
    dockerfile = (
        "FROM {imagename}\n" "COPY {build_context_path}/ {workdir}\n" "WORKDIR {workdir}\n"
    ).format(
        imagename=base_image,
        build_context_path=_PROJECT_TAR_ARCHIVE_NAME,
        workdir=MLFLOW_DOCKER_WORKDIR_PATH,
    )
    build_ctx_path = _create_docker_build_ctx(work_dir, dockerfile)
    with open(build_ctx_path, "rb") as docker_build_ctx:
        _logger.info("=== Building docker image %s ===", image_uri)
        client = docker.from_env()
        image, _ = client.images.build(
            tag=image_uri,
            forcerm=True,
            dockerfile=posixpath.join(_PROJECT_TAR_ARCHIVE_NAME, _GENERATED_DOCKERFILE_NAME),
            fileobj=docker_build_ctx,
            custom_context=True,
            encoding="gzip",
        )
    try:
        os.remove(build_ctx_path)
    except Exception:
        _logger.info("Temporary docker context file %s was not deleted.", build_ctx_path)
    tracking.MlflowClient().set_tag(run_id, MLFLOW_DOCKER_IMAGE_URI, image_uri)
    tracking.MlflowClient().set_tag(run_id, MLFLOW_DOCKER_IMAGE_ID, image.id)
    return image


def _get_docker_image_uri(repository_uri, work_dir):
    """
    Returns an appropriate Docker image URI for a project based on the git hash of the specified
    working directory.

    :param repository_uri: The URI of the Docker repository with which to tag the image. The
                           repository URI is used as the prefix of the image URI.
    :param work_dir: Path to the working directory in which to search for a git commit hash
    """
    repository_uri = repository_uri if repository_uri else "docker-project"
    # Optionally include first 7 digits of git SHA in tag name, if available.
    git_commit = _get_git_commit(work_dir)
    version_string = ":" + git_commit[:7] if git_commit else ""
    return repository_uri + version_string


def _create_docker_build_ctx(work_dir, dockerfile_contents):
    """
    Creates build context tarfile containing Dockerfile and project code, returning path to tarfile
    """
    directory = tempfile.mkdtemp()
    try:
        dst_path = os.path.join(directory, "mlflow-project-contents")
        shutil.copytree(src=work_dir, dst=dst_path)
        with open(os.path.join(dst_path, _GENERATED_DOCKERFILE_NAME), "w") as handle:
            handle.write(dockerfile_contents)
        _, result_path = tempfile.mkstemp()
        file_utils.make_tarfile(
            output_filename=result_path, source_dir=dst_path, archive_name=_PROJECT_TAR_ARCHIVE_NAME
        )
    finally:
        shutil.rmtree(directory)
    return result_path


def get_docker_tracking_cmd_and_envs(tracking_uri):
    cmds = []
    env_vars = dict()

    local_path, container_tracking_uri = _get_local_uri_or_none(tracking_uri)
    if local_path is not None:
        cmds = ["-v", "%s:%s" % (local_path, _MLFLOW_DOCKER_TRACKING_DIR_PATH)]
        env_vars[tracking._TRACKING_URI_ENV_VAR] = container_tracking_uri
    env_vars.update(get_databricks_env_vars(tracking_uri))
    return cmds, env_vars


def _get_local_uri_or_none(uri):
    if uri == "databricks":
        return None, None
    parsed_uri = urllib.parse.urlparse(uri)
    if not parsed_uri.netloc and parsed_uri.scheme in ("", "file", "sqlite"):
        path = urllib.request.url2pathname(parsed_uri.path)
        if parsed_uri.scheme == "sqlite":
            uri = file_utils.path_to_local_sqlite_uri(_MLFLOW_DOCKER_TRACKING_DIR_PATH)
        else:
            uri = file_utils.path_to_local_file_uri(_MLFLOW_DOCKER_TRACKING_DIR_PATH)
        return path, uri
    else:
        return None, None
