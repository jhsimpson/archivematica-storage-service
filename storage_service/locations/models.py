import datetime
import logging
import os.path
import subprocess

from django.core.exceptions import ValidationError
from django.db import models

from django_extensions.db.fields import UUIDField

import common.utils as utils

logger = logging.getLogger(__name__)
logging.basicConfig(filename="/tmp/storage-service.log",
    level=logging.INFO)

########################## SPACES ##########################

def validate_space_path(path):
    """ Validation for path in Space.  Must be absolute. """
    if path[0] != '/':
        raise ValidationError("Path must begin with a /")

class Space(models.Model):
    """ Common storage space information.

    Knows what protocol to use to access a storage space, but all protocol
    specific information is in children classes with ForeignKeys to Space."""
    uuid = UUIDField(editable=False, unique=True, version=4,
        help_text="Unique identifier")

    LOCAL_FILESYSTEM = 'FS'
    NFS = 'NFS'
    # LOCKSS = 'LOCKSS'
    # FEDORA = 'FEDORA'
    ACCESS_PROTOCOL_CHOICES = (
        (LOCAL_FILESYSTEM, "Local Filesystem"),
        (NFS, "NFS")
    )
    access_protocol = models.CharField(max_length=6,
                            choices=ACCESS_PROTOCOL_CHOICES,
                            help_text="How the space can be accessed.")
    size = models.BigIntegerField(default=None, null=True, blank=True,
                                  help_text="Size in bytes")
    used = models.BigIntegerField(default=0,
                                  help_text="Amount used in bytes")
    path = models.TextField(validators=[validate_space_path])
    verified = models.BooleanField(default=False,
       help_text="Whether or not the space has been verified to be accessible.")
    last_verified = models.DateTimeField(default=None, null=True, blank=True,
        help_text="Time this location was last verified to be accessible.")

    def __unicode__(self):
        return "{uuid}: {path} ({access_protocol})".format(
            uuid=self.uuid,
            access_protocol=self.access_protocol,
            path=self.path,
            )

    def store_aip(self, *args, **kwargs):
        # FIXME there has to be a better way to do this
        if self.access_protocol == self.LOCAL_FILESYSTEM:
            self.localfilesystem.store_aip(*args, **kwargs)
        elif self.access_protocol == self.NFS:
            self.nfs.store_aip(*args, **kwargs)
        else:
            logging.warning("No access protocol for this space.")


class LocalFilesystem(models.Model):
    """ Spaces found in the local filesystem."""
    space = models.OneToOneField('Space', to_field='uuid')
    # Does not currently need any other information - delete?

    def save(self, *args, **kwargs):
        self.verify()
        super(LocalFilesystem, self).save(*args, **kwargs)

    def verify(self):
        """ Verify that the space is accessible to the storage service. """
        # TODO run script to verify that it works
        verified = os.path.isdir(self.space.path)
        self.space.verified = verified
        self.space.last_verified = datetime.datetime.now()

    def store_aip(self, aip_file, *args, **kwargs):
        """ Stores aip_file in this space. """
        # IDEA Make this a script that can be run? Would lose access to python
        # objects and have to pass UUIDs

        # Confirm that this is the correct space to be moving to
        assert self.space == aip_file.current_location.space

        # TODO Move some of the procesing in archivematica
        # clientScripts/storeAIP to here
        source = aip_file.full_origin_path()

        # Store AIP at
        # destination_location/uuid/split/into/chunks/destination_path
        path = utils.uuid_to_path(aip_file.uuid)
        destination = os.path.join(
            aip_file.current_location.full_path(),
            path,
            aip_file.current_path)
        logging.info("rsyncing from {} to {}".format(source, destination))
        try:
            os.makedirs(os.path.dirname(destination))
        except OSError as e:
            # Errno 17 = folder exists already - expected error
            if e.errno != 17:
                logging.warning("Could not create storage directory: {}".format(e))
                return -1
        try:
            subprocess.call(['rsync', '-a', source, destination])
        except CalledProcessError as e:
            logging.warning("{}".format(e))
            return -1


class NFS(models.Model):
    """ Spaces accessed over NFS. """
    space = models.OneToOneField('Space', to_field='uuid')

    # Space.path is the local path
    remote_name = models.CharField(max_length=256, 
        help_text="Name of the NFS server.")
    remote_path = models.TextField(
        help_text="Path on the NFS server to the export.")
    version = models.CharField(max_length=64, default='nfs4', 
        help_text="Type of the filesystem, i.e. nfs, or nfs4. \
        Should match a command in `mount`.")
    # https://help.ubuntu.com/community/NFSv4Howto

    manually_mounted = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        self.verify()
        super(NFS, self).save(*args, **kwargs)

    def verify(self):
        """ Verify that the space is accessible to the storage service. """
        # TODO run script to verify that it works
        if self.manually_mounted:
            verified = os.path.ismount(self.space.path)
            self.space.verified = verified
            self.space.last_verified = datetime.datetime.now()

    def mount(self):
        """ Mount the NFS export with the provided info. """
        # sudo mount -t nfs -o proto=tcp,port=2049 192.168.1.133:/export /mnt/
        # sudo mount -t self.version -o proto=tcp,port=2049 self.remote_name:self.remote_path self.space.path
        # or /etc/fstab
        # self.remote_name:self.remote_path   self.space.path   self.version    auto,user  0  0
        # may need to tweak options
        pass

    def store_aip(self, aip_file, *args, **kwargs):
        """ Stores aip_file in this space, at aip_file.current_location.

        Assumes that aip_file.current_location is mounted locally."""
        # IDEA Make this a script that can be run? Would lose access to python
        # objects and have to pass UUIDs

        # Confirm that this is the correct space to be moving to
        assert self.space == aip_file.current_location.space

        # TODO Move some of the procesing in archivematica
        # clientScripts/storeAIP to here
        source = aip_file.full_origin_path()

        # Store AIP at
        # destination_location/uuid/split/into/chunks/destination_path
        path = utils.uuid_to_path(aip_file.uuid)
        destination = os.path.join(
            aip_file.current_location.full_path(),
            path,
            aip_file.current_path)
        logging.info("rsyncing from {} to {}".format(source, destination))
        try:
            os.makedirs(os.path.dirname(destination))
        except OSError as e:
            # Errno 17 = folder exists already - expected error
            if e.errno != 17:
                logging.warning("Could not create storage directory: {}".format(e))
                return -1
        try:
            subprocess.call(['rsync', '-a', source, destination])
        except CalledProcessError as e:
            logging.warning("{}".format(e))
            return -1

# To add a new storage space the following places must be updated:
#  locations/models.py (this file)
#   Add constant for storage protocol
#   Add constant to ACCESS_PROTOCOL_CHOICES
#   Add class for protocol-specific fields using template below
#  locations/forms.py
#   Add ModelForm for new class
#  common/constants.py
#   Add entry to protocol with fields that should be added to GET resource 
#     requests, the Model and ModelForm

# class Example(models.Model):
#     space = models.OneToOneField('Space', to_field='uuid')
#
#     def verify(self):
#         pass

########################## LOCATIONS ##########################

class EnabledLocations(models.Manager):
    """ Manager to only return enabled Locations. """
    def get_query_set(self):
        return super(EnabledLocations, self).get_query_set().filter(
            disabled=False)

class Location(models.Model):
    """ Stores information about a location. """

    uuid = UUIDField(editable=False, unique=True, version=4,
        help_text="Unique identifier")
    space = models.ForeignKey('Space', to_field='uuid')

    TRANSFER_SOURCE = 'TS'
    AIP_STORAGE = 'AS'
    # QUARANTINE = 'QU'
    # BACKLOG = 'BL'
    CURRENTLY_PROCESSING = 'CP'

    PURPOSE_CHOICES = (
        (TRANSFER_SOURCE, 'Transfer Source'),
        (AIP_STORAGE, 'AIP Storage'),
        # (QUARANTINE, 'Quarantine'),
        # (BACKLOG, 'Backlog Transfer'),
        (CURRENTLY_PROCESSING, 'Currently Processing'),
    )
    purpose = models.CharField(max_length=2,
        choices=PURPOSE_CHOICES,
        help_text="Purpose of the space.  Eg. AIP storage, Transfer source")

    relative_path = models.TextField()
    description = models.CharField(max_length=256, default=None,
        null=True, blank=True)
    quota = models.BigIntegerField(default=None, null=True, blank=True,
        help_text="Size in bytes")
    used = models.BigIntegerField(default=0,
        help_text="Amount used in bytes")
    disabled = models.BooleanField(default=False,
        help_text="True if space should no longer be accessed.")

    objects = models.Manager()
    enabled = EnabledLocations()

    def __unicode__(self):
        return "{uuid}: {path} ({purpose})".format(
            uuid=self.uuid,
            purpose=self.purpose,
            path=self.relative_path,
            )

    def full_path(self):
        """ Returns full path of location: space + location paths. """
        return os.path.join(self.space.path, self.relative_path)

    def get_description(self):
        """ Returns a user-friendly description (or the path). """
        return self.description or self.full_path()


########################## FILES ##########################

class File(models.Model):
    """ A file stored in a specific location. """
    uuid = UUIDField(editable=False, unique=True, version=4,
        help_text="Unique identifier")
    origin_location = models.ForeignKey(Location, to_field='uuid', related_name='+')
    origin_path = models.TextField()
    current_location = models.ForeignKey(Location, to_field='uuid', related_name='+')
    current_path = models.TextField()
    size = models.IntegerField(default=0)

    PACKAGE_TYPE_CHOICES = (
        ("AIP", 'AIP'),
        ("SIP", 'SIP'),
        ("DIP", 'DIP'),
        ("transfer", 'Transfer'),
        ("file", 'Single File'),
    )
    package_type = models.CharField(max_length=8,
        choices=PACKAGE_TYPE_CHOICES,
        help_text="Purpose of the space.  Eg. AIP storage, Transfer source")
    uploaded = models.BooleanField(default=False,
        help_text="Whether the file is at its final destination or not.")

    def __unicode__(self):
        return "{uuid}: {path} in {location}".format(
            uuid=self.uuid,
            path=self.current_path,
            location=self.current_location,
            )
        # return "File: {}".format(self.uuid)

    def full_path(self):
        return os.path.join(self.current_location.full_path(), self.current_path)

    def full_origin_path(self):
        return os.path.join(self.origin_location.full_path(), self.origin_path)