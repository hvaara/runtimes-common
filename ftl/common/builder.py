# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import tarfile
import logging
import httplib2
import json

from containerregistry.client import docker_creds
from containerregistry.client import docker_name
from containerregistry.client.v2_2 import append
from containerregistry.client.v2_2 import docker_image
from containerregistry.client.v2_2 import docker_session
from containerregistry.client.v2_2 import save
from containerregistry.transport import transport_pool

from ftl.common import cache
from ftl.common import ftl_util

_THREADS = 32


class Base(object):
    """Base is an abstract base class representing a container builder.
    It provides methods for generating runtime layers and an application
    layer.
    """

    __metaclass__ = abc.ABCMeta  # For enforcing that methods are overriden.

    def __init__(self, ctx):
        self._ctx = ctx

    @abc.abstractmethod
    def Build(self):
        """Build method encapsulates all layer building and image creation.
        """


class JustApp(Base):
    """JustApp is an implementation of a builder that has logic to build an
    application layer.
    """

    def __init__(self, ctx):
        super(JustApp, self).__init__(ctx)

    def Build(self):
        """Override."""
        # this can't be abstract as it is instantiated in tests
        return


class RuntimeBase(JustApp):
    """RuntimeBase is an abstract base class representing a container builder
    for runtime applications with dependencies.

    It provides methods for generating appending layers and caching images
    """

    def __init__(self, ctx, namespace, args, cache_version_str,
                 descriptor_files):
        super(RuntimeBase, self).__init__(ctx)
        self._namespace = namespace
        if args.entrypoint:
            args.entrypoint = args.entrypoint.split(" ")
        if args.exposed_ports:
            args.exposed_ports = args.exposed_ports.split(" ")
        self._args = args
        self._base_name = docker_name.Tag(self._args.base)
        self._base_creds = docker_creds.DefaultKeychain.Resolve(
            self._base_name)
        self._target_image = docker_name.Tag(self._args.name)
        self._target_creds = docker_creds.DefaultKeychain.Resolve(
            self._target_image)
        self._transport = transport_pool.Http(httplib2.Http, size=_THREADS)
        if args.tar_base_image_path:
            self._base_image = docker_image.FromTarball(
                args.tar_base_image_path)
        else:
            self._base_image = docker_image.FromRegistry(
                self._base_name, self._base_creds, self._transport)
        self._base_image.__enter__()
        cache_repo = args.cache_repository
        if not cache_repo:
            cache_repo = self._target_image.as_repository()
        self._cache = cache.Registry(
            repo=cache_repo,
            namespace=self._namespace,
            base_image=self._base_image,
            creds=self._target_creds,
            transport=self._transport,
            cache_version=cache_version_str,
            threads=_THREADS,
            mount=[self._base_name],
            use_global=self._args.global_cache)
        self._descriptor_files = descriptor_files

    def Build(self):
        return

    def AppendLayersIntoImage(self, lyr_imgs):
        for i, lyr_img in enumerate(lyr_imgs):
            if i == 0:
                result_image = lyr_img
                continue
            img = lyr_img.GetImage()
            diff_ids = img.diff_ids()
            for diff_id in diff_ids:
                lyr = img.blob(img._diff_id_to_digest(diff_id))
                overrides = ftl_util.CfgDctToOverrides(
                    json.loads(img.config_file()))

                result_image = append.Layer(
                    result_image, lyr, diff_id=diff_id, overrides=overrides)
        return result_image

    def StoreImage(self, result_image):
        if self._args.output_path:
            with ftl_util.Timing("saving_tarball_image"):
                with tarfile.open(
                        name=self._args.output_path, mode='w') as tar:
                    save.tarball(self._target_image, result_image, tar)
                logging.info("{0} tarball located at {1}".format(
                    str(self._target_image), self._args.output_path))
            return
        if self._args.upload:
            with ftl_util.Timing("pushing_image_to_docker_registry"):
                with docker_session.Push(
                        self._target_image,
                        self._target_creds,
                        self._transport,
                        threads=_THREADS,
                        mount=[self._base_name]) as session:
                    logging.info('Pushing final image...')
                    session.upload(result_image)
                return
