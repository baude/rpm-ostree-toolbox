#!/usr/bin/env python
# Copyright (C) 2014 Colin Walters <walters@verbum.org>, Andy Grimm <agrimm@redhat.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import json
import os
import sys
import tempfile
import argparse
import shutil
import subprocess
import distutils.spawn
from gi.repository import Gio, OSTree, GLib
from iniparse import INIConfig

from imgfac.PersistentImageManager import PersistentImageManager

# For ImageFactory builds
from imgfac.BuildDispatcher import BuildDispatcher
from imgfac.PluginManager import PluginManager
from imgfac.ApplicationConfiguration import ApplicationConfiguration
import logging

from .taskbase import TaskBase

from .utils import run_sync, fail_msg


class ImgBuilder(object):
    '''
    Abstract class from which specific builder inherit.
    This is mostly because we want to allow for direct calls to imagefactory,
    but also calls to koji.  In one case, we need to generate a TDL; in the
    other, we provide the parameters and let the system construct it.
    '''

    def __init__(self, *args, **kwargs):
        pass

    def build(self):
        '''
        Trigger a build.  Return something useful like a build id, status, etc.
        '''
        raise NotImplementedError

    def download(self, dest):
        '''
        Copy/download artifacts to a destination
        '''
        raise NotImplementedError


class ImgFacBuilder(ImgBuilder):
    def __init__(self, *args, **kwargs):
        config = json.loads(open('/etc/imagefactory/imagefactory.conf').read())
        config['plugins'] = '/etc/imagefactory/plugins.d'
        config['timeout'] = 3600
        config['rhevm_image_format'] = 'qcow2'
        ApplicationConfiguration(configuration=config)
        plugin_mgr = PluginManager('/etc/imagefactory/plugins.d')
        plugin_mgr.load()

        self.fhandler = logging.StreamHandler(sys.stdout)
        self.tlog = logging.getLogger()
        self.tlog.setLevel(logging.DEBUG)
        self.tlog.addHandler(self.fhandler)
 
        global verbosemode
        if verbosemode:
            ch = logging.StreamHandler(sys.stdout)
            ch.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            ch.setFormatter(formatter)
            self.tlog.addHandler(ch)

        pass

    def build(self, template=None, parameters=None):
        bd = BuildDispatcher()
        builder = bd.builder_for_base_image(template=template,
                                            parameters=parameters)
        print json.dumps(builder.app_config)
        image = builder.base_image
        thread = builder.base_thread
        for key in image.metadata():
            print "%s %s" % (key, getattr(image, key, None))

        thread.join()

        if image.status != "COMPLETE":
            fail_msg("Failed image status: " + image.status)
        return image

    def buildimagetype(self, imagetype, baseid, imgopts={}):
        """
        This method compliments the builder method by taking its
        uuid and outputputting various image formats
        """
        print "Working on a {0} for {1}".format(imagetype, baseid)
        bd = BuildDispatcher()
        imagebuilder = bd.builder_for_target_image(imagetype, image_id=baseid, template=None, parameters=imgopts)
        target_image = imagebuilder.target_image
        thread = imagebuilder.target_thread
        thread.join()
        if target_image.status != "COMPLETE":
            fail_msg("Failed image status: " + target_image.status)

        print target_image.identifier
        # Now doing the OVA

        print "Creating OVA for {0}".format(imagetype)

        bdi = BuildDispatcher()
        ovabuilder = bdi.builder_for_target_image("ova", image_id=target_image.identifier, template=None, parameters=imgopts)
        target_ova = ovabuilder.target_image
        ovathread = ovabuilder.target_thread
        ovathread.join()
        if target_ova.status != "COMPLETE":
            fail_msg("Failed image status: " + target_ova.status)
        return target_ova

    def download(self):
        pass


class KojiBuilder(ImgBuilder):
    def __init__(self, **kwargs):
        # sort of
        # server = kwargs.pop('server')
        # self.session = koji.ClientSession(server, kwargs)
        pass

    def build(self):
        # TODO: populate buildinfo
        # self.session.createImageBuild(buildinfo)
        pass

    def download(self):
        pass


class ImageFactoryTask(TaskBase):
    def create(self, outputdir, name, ksfile, tdl, imageouttypes):
        self._name = name
        self._tdl = tdl
        self._kickstart = ksfile 
        imgfunc = ImageFunctions()

        [res, rev] = self.repo.resolve_rev(self.ref, False)
        [res, commit] = self.repo.load_variant(OSTree.ObjectType.COMMIT, rev)

        commitdate = GLib.DateTime.new_from_unix_utc(OSTree.commit_get_timestamp(commit)).format("%c")
        print commitdate

        target = os.path.join(outputdir, '%s.raw' % (self._name))

        port_file_path = self.workdir + '/repo-port'
        subprocess.check_call(['ostree',
                               'trivial-httpd', '--autoexit', '--daemonize',
                               '--port-file', port_file_path],
                              cwd=self.ostree_repo)

        httpd_port = open(port_file_path).read().strip()
        print "trivial httpd port=%s" % (httpd_port, )

        ks_basename = os.path.basename(ksfile)
        flattened_ks = os.path.join(self.workdir, ks_basename)

        # FIXME - eventually stop hardcoding this via some mapping
        if ks_basename.find('fedora') >= 0:
            kickstart_version = 'F21'
        else:
            kickstart_version = 'RHEL7'
        run_sync(['ksflatten', '--version', kickstart_version,
                  '-c', ksfile, '-o', flattened_ks])

        # TODO: Pull kickstart from separate git repo
        ksdata = open(flattened_ks).read()
        substitutions = { 'OSTREE_PORT': httpd_port,
                          'OSTREE_REF':  self.ref,
                          'OSTREE_OSNAME':  self.os_name }
        for subname, subval in substitutions.iteritems():
            ksdata = ksdata.replace('@%s@' % (subname, ), subval)

        imgfunc.checkoz()

        parameters =  { "install_script": ksdata,
                        "generate_icicle": False,
                        "oz_overrides": json.dumps(imgfunc.ozoverrides)
                      }
        print "Starting build"
        image = self.builder.build(template=open(self._tdl).read(), parameters=parameters)

        # For debug, you can comment out the above and enable the code below
        # to skip the initial image creation.  Just point myuuid at the proper
        # image uuid

        # self.builder.download()
        # myuuid = "4755687a-2c98-4466-a571-42f57805f690"
        # pim = PersistentImageManager.default_manager()
        # image = pim.image_with_id(myuuid)

        # Copy the qcow2 file to the outputdir
        outfile = os.path.join(outputdir, '%s.qcow2' % (self._name))
        shutil.copyfile(image.data, outfile)

        if 'raw' in imageouttypes:
            print "Processing image from qcow2 to raw"
            print image.data
            outputname = os.path.join(outputdir, '%s.raw' % (self._name))
            print outputname
            qemucmd = ['qemu-img', 'convert', '-f', 'raw', '-O', 'qcow2', image.data, outputname]
            imageouttypes.pop(imageouttypes.index("raw"))
            subprocess.check_call(qemucmd)
            shutil.copyfile(image.data, target)
            print "Created: " + target

        for imagetype in imageouttypes:
            if (imagetype == 'vsphere') or (imagetype == 'rhevm'):
                print "Creating {0} image".format(imagetype)
                target_image = self.builder.buildimagetype(imagetype, image.identifier)
                infile = target_image.data
                outfile = os.path.join(outputdir, '%s-%s.ova' % (self._name, imagetype))
                shutil.copyfile(infile, outfile)

    @property
    def builder(self):
        # TODO: option to switch to koji builder
        if True:
            return ImgFacBuilder(workdir=self.workdir)
        else:
            return KojiBuilder()

## End Composer

class ImageFunctions(object):
    def __init__(self):
        self.ozoverrides = {}
        self.cfg = INIConfig(open('/etc/oz/oz.cfg'))

    def addozoverride(self, cfgsec, key, value):
        """
        Method that takes oz config section and adds a key
        and value to prepare an json formatted oz override
        value
        """
        if cfgsec not in self.ozoverrides.keys():
            self.ozoverrides[cfgsec] = {}
        self.ozoverrides[cfgsec][key] = value

    def checkoz(self):
        """
        Method which checks the oz.cfg for certain variables to alert
        user to potential errors caused by the cfg itself. It also
        returns the default image type.
        """
        cfg = INIConfig(open('/etc/oz/oz.cfg'))

        # Set default image to always be KVM
        self.addozoverride('libvirt', 'image_type', 'qcow2')

        # iniparse returns an object if it cannot find the config option
        # we check if the the return is a str and assume if not, it does
        # not exist

        if int(cfg.libvirt.memory) < 2048:
            print "Your current oz configuration specifies a memory amount of less than 2048 which can lead to possible image creation failures. Overriding temporarily to 2048"
            self.addozoverride('libvirt', 'memory', 2048)



def parseimagetypes(imagetypes):
    default_image_types = ["kvm", "raw", "vsphere", "rhevm"]
    if imagetypes == None:
        return default_image_types

    # Check that input types are valid
    for i in imagetypes:
        if i not in default_image_types:
            print "{0} is not a valid image type.  The valid types are {1}".format(i, default_image_types)
            exit(1) 

    return imagetypes


def main():
    parser = argparse.ArgumentParser(description='Use ImageFactory to create a disk image')
    parser.add_argument('-c', '--config', default='config.ini', type=str, help='Path to config file')
    parser.add_argument('-i', '--images', help='Output image formats in list format', action='append')
    parser.add_argument('--name', type=str, required=True, help='Image name') 
    parser.add_argument('--tdl', type=str, required=True, help='TDL file') 
    parser.add_argument('-o', '--outputdir', type=str, required=True, help='Path to image output directory')
    parser.add_argument('-k', '--kickstart', type=str, required=True, help='Path to kickstart') 
    parser.add_argument('-r', '--release', type=str, default='rawhide', help='Release to compose (references a config file section)')
    parser.add_argument('-v', '--verbose', action='store_true', help='verbose output')
    args = parser.parse_args()
     
    imagetypes = parseimagetypes(args.images)

    composer = ImageFactoryTask(args.config, release=args.release)
    composer.show_config()
    global verbosemode
    verbosemode = args.verbose
    try:
        composer.create(outputdir=args.outputdir,
                        name=args.name,
                        ksfile=args.kickstart,
                        tdl=args.tdl,
                        imageouttypes=imagetypes
                        )
    finally:
        composer.cleanup()
