import os
import json
import sys
import time
import zipfile
import docker
import logging
import requests
import subprocess
import multiprocessing

from . import config as docker_config
from pywren_ibm_cloud.utils import version_str
from pywren_ibm_cloud.version import __version__
from pywren_ibm_cloud.config import TEMP, DOCKER_FOLDER
from pywren_ibm_cloud.compute.utils import create_function_handler_zip

logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


class DockerBackend:
    """
    A wrap-up around Docker APIs.
    """

    def __init__(self, docker_config):
        self.log_level = os.getenv('PYWREN_LOGLEVEL')
        self.config = docker_config
        self.name = 'docker'
        self.host = docker_config['host']
        self.queue = multiprocessing.Queue()
        self._is_localhost = self.host in ['127.0.0.1', 'localhost']
        self.docker_client = docker.from_env()

        log_msg = 'PyWren v{} init for Docker - Host: {}'.format(__version__, self.host)
        logger.info(log_msg)
        if not self.log_level:
            print(log_msg)

    def _format_runtime_name(self, docker_image_name):
        name = docker_image_name.replace('/', '_').replace(':', '_')
        return 'pywren_{}'.format(name)

    def _unformat_runtime_name(self, runtime_name):
        image_name = runtime_name.replace('pywren_', '')
        image_name = image_name.replace('_', '/', 1)
        image_name = image_name.replace('_', ':', -1)
        return image_name, None

    def _get_default_runtime_image_name(self):
        python_version = version_str(sys.version_info)
        return docker_config.RUNTIME_DEFAULT[python_version]

    def _delete_function_handler_zip(self):
        os.remove(docker_config.FH_ZIP_LOCATION)

    def _init_runtime(self, docker_image_name):
        name = self._format_runtime_name(docker_image_name)
        uid_cmd = "id -u $USER"

        if self._is_localhost:
            running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
            running_runtimes = [c.name for c in running_containers]
            uid = subprocess.check_output(uid_cmd, shell=True).decode().strip()
            if name not in running_runtimes:
                self.docker_client.containers.run(docker_image_name, entrypoint='python',
                                                  command='{}/__main__.py'.format(DOCKER_FOLDER),
                                                  volumes=['{}:/tmp'.format(TEMP)],
                                                  detach=True, auto_remove=True,
                                                  user=uid, name=name,
                                                  ports={'8080/tcp': docker_config.PYWREN_SERVER_PORT})
                time.sleep(5)

        else:
            running_runtimes_cmd = "docker ps --format '{{.Names}}' -f name=pywren"
            running_runtimes = subprocess.run(running_runtimes_cmd, shell=True, stdout=subprocess.PIPE).stdout.decode()
            cmd = ('docker run -d --name pywren_{} --user {} -v /tmp:/tmp -p 8080:8080'
                   ' --entrypoint "python" {} /tmp/pywren.docker/__main__.py >/dev/null 2>&1'
                   .format(name, uid, docker_image_name))
            res = os.system(cmd)
            if res != 0:
                raise Exception('There was an error starting the runtime')
            time.sleep(5)
            pass

    def _generate_runtime_meta(self, docker_image_name):
        """
        Extracts installed Python modules from the local machine
        """
        self._init_runtime(docker_image_name)

        r = requests.get('http://{}:{}/preinstalls'.format(self.host, docker_config.PYWREN_SERVER_PORT))
        runtime_meta = r.json()

        if not runtime_meta or 'preinstalls' not in runtime_meta:
            raise Exception(runtime_meta)

        return runtime_meta

    def invoke(self, docker_image_name, memory, payload):
        """
        Invoke the function with the payload. runtime_name and memory
        are not used since it runs in the local machine.
        """
        self._init_runtime(docker_image_name)
        r = requests.post("http://{}:{}/".format(self.host, docker_config.PYWREN_SERVER_PORT), data=json.dumps(payload))
        response = r.json()
        return response['activationId']

    def create_runtime(self, docker_image_name, memory, timeout):
        """
        Pulls the docker image from the docker hub and copies
        the necessary files to the host.
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        create_function_handler_zip(docker_config.FH_ZIP_LOCATION, '__main__.py', __file__)

        if self._is_localhost:
            os.makedirs(DOCKER_FOLDER, exist_ok=True)

            archive = zipfile.ZipFile(docker_config.FH_ZIP_LOCATION)
            for file in archive.namelist():
                archive.extract(file, DOCKER_FOLDER)

            self.docker_client.images.pull(docker_image_name)

        else:
            cmd = 'docker pull {} >/dev/null 2>&1'.format(docker_image_name)
            res = os.system(cmd)
            if res != 0:
                raise Exception('There was an error pulling the runtime')

        self._delete_function_handler_zip()
        runtime_meta = self._generate_runtime_meta(docker_image_name)

        return runtime_meta

    def build_runtime(self, docker_image_name, dockerfile):
        """
        Builds a new runtime from a Dockerfile
        """
        raise Exception('You must use an IBM CF/knative built runtime')

    def delete_runtime(self, docker_image_name, memory):
        """
        Deletes a runtime
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        logger.debug('Deleting {} runtime'.format(docker_image_name))
        name = self._format_runtime_name(docker_image_name)
        if self._is_localhost:
            self.docker_client.containers.stop(name, force=True)
        else:
            cmd = 'docker rm -f {} >/dev/null 2>&1'.format(name)
            os.system(cmd)

    def delete_all_runtimes(self):
        """
        Delete all created runtimes
        """
        if self._is_localhost:
            running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
            for runtime in running_containers:
                logger.debug('Deleting {} runtime'.format(runtime.name))
                runtime.stop()
        else:
            list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
            running_containers = subprocess.check_output(list_runtimes_cmd, shell=True).decode().strip()
            for name in running_containers.splitlines():
                cmd = 'docker rm -f {} >/dev/null 2>&1'.format(name)
                os.system(cmd)

    def list_runtimes(self, docker_image_name='all'):
        """
        List all the runtimes deployed in the local machine
        return: list of tuples (docker_image_name, memory)
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        runtimes = []

        if self._is_localhost:
            running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
            running_runtimes = [c.name for c in running_containers]
        else:
            list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
            running_containers = subprocess.check_output(list_runtimes_cmd, shell=True).decode().strip()
            running_runtimes = running_containers.splitlines()

        for runtime in running_runtimes:
            name = self._format_runtime_name(docker_image_name)
            if name == runtime or docker_image_name == 'all':
                tag = self._unformat_runtime_name(runtime)
                runtimes.append((tag, None))

        return runtimes

    def get_runtime_key(self, docker_image_name, memory):
        """
        Method that creates and returns the runtime key.
        Runtime keys are used to uniquely identify runtimes within the storage,
        in order to know what runtimes are installed and what not.
        """
        runtime_name = self._format_runtime_name(docker_image_name)
        runtime_key = os.path.join(self.name, self.host, runtime_name)

        return runtime_key
