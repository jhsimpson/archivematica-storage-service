# stdlib, alphabetical
import datetime
import json
from lxml import etree as etree
from multiprocessing import Process
import os
import shutil
import tempfile
import time
import traceback
import urllib
import urllib2

# Core Django, alphabetical
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone

# External dependencies, alphabetical
from annoying.functions import get_object_or_None

# This project, alphabetical
from locations.models import Location
from locations.models import Pipeline
from locations.models import Space
from locations.models import SwordServer
from locations.models import LocationDownloadTask
import helpers

"""
Return deposit status, indicating whether any incomplete or failed batch
downloads exist.
"""
# TODO: move to helpers
def deposit_downloading_status(deposit_uuid):
    deposit = helpers.get_deposit(deposit_uuid)
    tasks = LocationDownloadTask.objects.filter(location=deposit)
    if len(tasks) > 0:
        # check each task for completion and failure
        complete = True
        failed = False

        for task in tasks:
            if task.downloading_status() != 'complete':
                complete = False
                if task.downloading_status() == 'failed':
                    failed = True
        if failed:
            return 'failed'
        else:
            if complete:
                return 'complete'
            else:
                return 'incomplete'
    else:
        return 'complete'

"""
Spawn an asynchrnous batch download
"""
# TODO: move to helpers
def spawn_download_task(deposit_uuid, object_content_urls):
    p = Process(target=_fetch_content, args=(deposit_uuid, object_content_urls))
    p.start()

"""
Download a number of files, keeping track of progress and success using a
database record. After downloading, finalize deposit if requested.
"""
# TODO: move to helpers
def _fetch_content(deposit_uuid, object_content_urls):
    # add download task to keep track of progress
    deposit = helpers.get_deposit(deposit_uuid)
    task = LocationDownloadTask(location=deposit)
    task.downloads_attempted = len(object_content_urls)
    task.downloads_completed = 0
    task.save()

    # download the files
    temp_dir = tempfile.mkdtemp()

    completed = 0
    for url in object_content_urls:
        try:
            filename = helpers.download_resource(url, temp_dir)
            shutil.move(os.path.join(temp_dir, filename),
                os.path.join(deposit.full_path(), filename))
            completed += 1
        except:
            pass

    # remove temp dir
    shutil.rmtree(temp_dir)

    # record the number of successful downloads and completion time
    task.downloads_completed = completed
    task.download_completion_time = timezone.now()
    task.save()

    # if the deposit is ready for finalization and this is the last batch
    # download to complete, then finalize
    if deposit.ready_for_finalization and deposit_downloading_status(deposit_uuid) == 'complete':
        _finalize_if_not_empty(deposit_uuid)

"""
Example GET of service document:

  curl -v http://127.0.0.1:8000/api/v1/sword/
"""
def service_document(request):
    spaces = Space.objects.filter(access_protocol='SWORD_S')

    collections = []
    for space in spaces:
        title = 'Collection'

        col_iri = request.build_absolute_uri(
            reverse('sword_collection', kwargs={'api_name': 'v1',
                'resource_name': 'space', 'uuid': space.uuid}))

        collections.append({
            'title': title,
            'url': col_iri
        })

    service_document_xml = render_to_string('locations/api/sword/service_document.xml', locals())
    response = HttpResponse(service_document_xml)
    response['Content-Type'] = 'application/atomserv+xml'
    return response

"""
Example GET of collection deposit list:

  curl -v http://localhost:8000/api/v1/space/96606387-cc70-4b09-b422-a7220606488d/sword/collection/

Example POST creation of deposit, allowing asynchronous downloading of object content URLs:

  curl -v -H "In-Progress: true" --data-binary @mets.xml --request POST http://localhost:8000/api/v1/space/96606387-cc70-4b09-b422-a7220606488d/sword/collection/

Example POST creation of deposit, finalizing the deposit and auto-approving it:

  curl -v -H "In-Progress: false" --data-binary @mets.xml --request POST http://localhost:8000/api/v1/space/c0bee7c8-3e9b-41e3-8600-ee9b2c475da2/sword/collection/
"""
def collection(request, space_uuid):
    space = get_object_or_None(Space, uuid=space_uuid)

    if space == None:
        return _sword_error_response(request, 404, 'Space {uuid} does not exist.'.format(uuid=space_uuid))

    if request.method == 'GET':
        # return list of deposits as ATOM feed
        col_iri = request.build_absolute_uri(
            reverse('sword_collection', kwargs={'api_name': 'v1',
                'resource_name': 'space', 'uuid': space_uuid}))

        feed = {
            'title': 'Deposits',
            'url': col_iri
        }

        entries = []

        for uuid in helpers.deposit_list(space_uuid):
            deposit = helpers.get_deposit(uuid)

            edit_iri = request.build_absolute_uri(
                reverse('sword_deposit', kwargs={'api_name': 'v1',
                    'resource_name': 'location', 'uuid': uuid}))

            entries.append({
                'title': deposit.description,
                'url': edit_iri,
            })

        collection_xml = render_to_string('locations/api/sword/collection.xml', locals())
        response = HttpResponse(collection_xml)
        response['Content-Type'] = 'application/atom+xml;type=feed'
        return response
    elif request.method == 'POST':
        # has the In-Progress header been set?
        if 'HTTP_IN_PROGRESS' in request.META:
            # process creation request, if criteria met
            source_location = request.GET.get('source_location', '')
            relative_path_to_files = request.GET.get('relative_path_to_files', '')
            
            if request.body != '':
                try:
                    mets_data = _parse_name_and_content_urls_from_request_body(request)

                    if mets_data != None:
                        if mets_data['deposit_name'] == None:
                            return _sword_error_response(request, 400, 'No deposit name found in XML.')
                        else:
                            # assemble deposit specification
                            deposit_specification = {'space_uuid': space_uuid}
                            deposit_specification['name'] = mets_data['deposit_name']
                            if 'HTTP_ON_BEHALF_OF' in request.META:
                                # TODO: should get this from author header or provided XML metadata
                                deposit_specification['sourceofacquisition'] = request.META['HTTP_ON_BEHALF_OF']

                            if not os.path.isdir(space.path):
                                return  _sword_error_response(request, 500, 'Space path (%s) does not exist: contact an administrator.' % (space.path))
                            else:
                                deposit_uuid = _create_deposit_directory_and_db_entry(deposit_specification)

                                if deposit_uuid != None:
                                    _spawn_batch_download_and_flag_finalization_if_requested(deposit_uuid, request, mets_data)

                                    if request.META['HTTP_IN_PROGRESS'] == 'true':
                                        return _deposit_receipt_response(request, deposit_uuid, 201)
                                    else:
                                        return _deposit_receipt_response(request, deposit_uuid, 200)
                                else:
                                    return _sword_error_response(request, 500, 'Could not create deposit: contact an administrator.')
                    else:
                        return _sword_error_response(request, 412, 'Error parsing XML ({error_message}).'.format(error_message=str(e)))
                except Exception as e:
                    return _sword_error_response(request, 400, traceback.format_exc())
            elif source_location != '' or relative_path_to_files != '':
                if source_location == '' or relative_path_to_files == '':
                    return HttpResponse('error both must be set') # TODO: should be HTTP 400
                else:
                    # TODO: consider removing this?
                    result = deposit_from_location_relative_path(source_location, relative_path_to_files, space.uuid)
                    if 'error' in result and result['error'] != None:
                        return _sword_error_response(request, 500, result['message'])
                    else:
                        return _deposit_receipt_response(request, result['deposit_uuid'], 200)
            else:
                return _sword_error_response(request, 412, 'A request body must be sent when creating a deposit.')
        else:
            return _sword_error_response(request, 412, 'The In-Progress header must be set to either true or false when creating a deposit.')
    else:
        return _sword_error_response(request, 405, 'This endpoint only responds to the GET and POST HTTP methods.')

def deposit_from_location_relative_path(source_location, relative_path_to_files, space_uuid=None, pipeline_uuid=None):
    # if no explicit space or pipeline specified, nothing can be done
    if space_uuid == None and pipeline_uuid == None:
        return

    # if a pipeline, but no space, was specified then look up the first SWORD server space
    # associated with the pipeline
    if space_uuid == None:
        pipeline = Pipeline.objects.get(uuid=pipeline_uuid)
        sword_server = SwordServer.objects.filter(pipeline=pipeline)[0]
        space_uuid = sword_server.space.uuid
    else:
    # ...otherwise, get the pipeline associated with the SWORD server space
        space = Space.objects.get(uuid=space_uuid)
        sword_server = SwordServer.objects.get(space=space)
        pipeline = sword_server.pipeline

    # a deposit of files stored on the storage server is being done
    location = Location.objects.get(uuid=source_location)
    path_to_deposit_files = os.path.join(location.full_path(), relative_path_to_files)

    deposit_specification = {'space_uuid': space_uuid}
    deposit_specification['name'] = os.path.basename(path_to_deposit_files) # replace this with optional name
    deposit_specification['source_path'] = path_to_deposit_files

    deposit_uuid = _create_deposit_directory_and_db_entry(deposit_specification)
    deposit = helpers.get_deposit(deposit_uuid)

    result =  _activate_transfer_and_request_approval_from_pipeline(deposit, sword_server)
    result['deposit_uuid'] = deposit_uuid
    return result

"""
Rename this function...

Spawn a batch download, optionally setting finalization beforehand.

If HTTP_IN_PROGRESS is set to true, spawn async batch download
"""
def _spawn_batch_download_and_flag_finalization_if_requested(deposit_uuid, request, mets_data):
    if request.META['HTTP_IN_PROGRESS'] == 'false':
        # Indicate that the deposit is ready for finalization (after all batch
        # downloads have completed)
        deposit = helpers.get_deposit(deposit_uuid)
        deposit.ready_for_finalization = True
        deposit.save()

    # create subprocess so content URLs can be downloaded asynchronously
    spawn_download_task(deposit_uuid, mets_data['object_content_urls'])

# returns None if didn't parse correctly
def _parse_name_and_content_urls_from_request_body(request):
    temp_filepath = helpers.write_request_body_to_temp_file(request)

    # parse name and content URLs out of XML
    try:
        mets_data = _parse_name_and_content_urls_from_mets_file(temp_filepath)
        return mets_data
    except etree.XMLSyntaxError as e:
        return None

def _parse_name_and_content_urls_from_mets_file(filepath):
    tree = etree.parse(filepath)
    root = tree.getroot()
    deposit_name = root.get('LABEL')

    # parse XML for content URLs
    object_content_urls = []

    elements = root.iterfind("{http://www.loc.gov/METS/}fileSec/"
        + "{http://www.loc.gov/METS/}fileGrp[@ID='DATASTREAMS']/"
        + "{http://www.loc.gov/METS/}fileGrp[@ID='OBJ']/"
        + "{http://www.loc.gov/METS/}file/"
        + "{http://www.loc.gov/METS/}FLocat"
    )

    for element in elements:
       object_content_urls.append(element.get('{http://www.w3.org/1999/xlink}href'))

    return {
        'deposit_name': deposit_name,
        'object_content_urls': object_content_urls
    }

"""
Create a new deposit location from a specification, optionally copying
files to it from a source path.
"""
def _create_deposit_directory_and_db_entry(deposit_specification):
    # Formulate deposit name using specification
    if 'name' in deposit_specification:
        deposit_name = deposit_specification['name']
    else:
        deposit_name = 'Untitled'

    # Formulate deposit path using space path and deposit name
    space = Space.objects.get(uuid=deposit_specification['space_uuid']) 
    deposit_path = os.path.join(
        space.path,
        deposit_name
    )

    # Pad deposit path, if it already exists, and either copy source data to it or just create it
    deposit_path = helpers.pad_destination_filepath_if_it_already_exists(deposit_path)
    if 'source_path' in deposit_specification and deposit_specification['source_path'] != '':
        shutil.copytree(deposit_specification['source_path'], deposit_path)
    else:
        os.mkdir(deposit_path)
        os.chmod(deposit_path, 02770) # drwxrws---

    # Create SWORD deposit location using deposit name and path
    if os.path.exists(deposit_path):
        deposit = Location.objects.create(description=deposit_name, relative_path=os.path.basename(deposit_path),
            space=space, purpose=Location.SWORD_DEPOSIT)

        # TODO: implement this
        if 'sourceofacquisition' in deposit_specification:
            deposit.source = deposit_specification['sourceofacquisition']

        deposit.save()
        return deposit.uuid

"""
Example POST finalization of deposit:

  curl -v -H "In-Progress: false" --request POST http://127.0.0.1:8000/api/v1/location/149cc29d-6472-4bcf-bee8-f8223bf60580/sword/

Example DELETE of deposit:

  curl -v -XDELETE http://127.0.0.1:8000/api/v1/location/149cc29d-6472-4bcf-bee8-f8223bf60580/sword/
"""
def deposit_edit(request, uuid):
    deposit = helpers.get_deposit(uuid)

    if deposit == None:
        return _sword_error_response(request, 404, 'Deposit location {uuid} does not exist.'.format(uuid=uuid))

    if deposit.has_been_submitted_for_processing():
        return _sword_error_response(request, 400, 'This deposit has already been submitted for processing.')

    if request.method == 'GET':
        deposit = helpers.get_deposit(uuid)
        edit_iri = request.build_absolute_uri(
            reverse(
                'sword_deposit',
                kwargs={'api_name': 'v1', 'resource_name': 'location', 'uuid': deposit.uuid}))

        entry = {
            'title': deposit.description,
            'url': edit_iri
        }
        response = HttpResponse(render_to_string('locations/api/sword/entry.xml', locals()))
        response['Content-Type'] = 'application/atom+xml'
        return response
    elif request.method == 'POST':
        # If METS XML has been sent to indicate a list of files needing downloading, handle it
        if request.body != '':
            mets_data = _parse_name_and_content_urls_from_request_body(request)
            if mets_data != None:
                _spawn_batch_download_and_flag_finalization_if_requested(uuid, request, mets_data)
                return _deposit_receipt_response(request, uuid, 200)
            else:
                return _sword_error_response(request, 412, 'Error parsing XML ({error_message}).'.format(error_message=str(e)))
        else:
            # Attempt to finalize (if requested), otherwise just return deposit receipt
            if 'HTTP_IN_PROGRESS' in request.META and request.META['HTTP_IN_PROGRESS'] == 'false':
                return _finalize_or_mark_for_finalization(request, uuid)
            else:
                return _deposit_receipt_response(request, uuid, 200)
    elif request.method == 'PUT':
        # TODO: implement update deposit
        return HttpResponse(status=204) # No content
    elif request.method == 'DELETE':
        # delete deposit files
        shutil.rmtree(deposit.full_path())

        # delete deposit
        deposit = helpers.get_deposit(uuid)
        deposit.delete()

        return HttpResponse(status=204) # No content
    else:
        return _sword_error_response(request, 405, 'This endpoint only responds to the GET, POST, PUT, and DELETE HTTP methods.')

#
#
#  return True if completed successfully
#
def _finalize_if_not_empty(deposit_uuid):
    deposit = helpers.get_deposit(deposit_uuid)
    if len(os.listdir(deposit.full_path())) > 0:
        # get sword server so we can access pipeline information
        sword_server = SwordServer.objects.get(space=deposit.space)
        result = _activate_transfer_and_request_approval_from_pipeline(deposit, sword_server)
        #result['deposit_uuid'] = deposit_uuid

        if 'error' in result:
            return _sword_error_response(request, 500, result['message'])

        # mark deposit as complete and return deposit receipt
        deposit.deposit_completion_time = timezone.now()
        deposit.save()

        return True
    else:
        return False

def _finalize_or_mark_for_finalization(request, deposit_uuid):
    if 'HTTP_IN_PROGRESS' in request.META and request.META['HTTP_IN_PROGRESS'] == 'false':
        if deposit_downloading_status(deposit_uuid) == 'complete':
            completed = _finalize_if_not_empty(deposit_uuid)
            if completed:
                return _deposit_receipt_response(request, deposit_uuid, 200)
            else:
                return _sword_error_response(request, 400, 'This deposit contains no files.')
        else:
            # Indicate that the deposit is ready for finalization (after all batch
            # downloads have completed
            deposit = helpers.get_deposit(deposit_uuid)
            deposit.ready_for_finalization = True
            deposit.save()

            return _deposit_receipt_response(request, uuid, 203) # change to 200
    else:
        return _sword_error_response(request, 400, 'The In-Progress header must be set to false when starting deposit processing.')

"""
Handle requesting the approval of a transfer from a pipeline via a REST call.

This function returns a dict representation of the results, either returning
the JSON returned by the request to the pipeline (converted to a dict) or
a dict indicating a pipeline authentication issue.

The dict representation is of the form:

{
    'error': <True|False>,
    'message': <description of success or failure>
}
"""
def _activate_transfer_and_request_approval_from_pipeline(deposit, sword_server):
    # make sure pipeline API access is configured
    for property in ['remote_name', 'api_username', 'api_key']:
        if getattr(sword_server.pipeline, property)=='':
            property_description = property.replace('_', ' ')
            # TODO: fix this
            return _sword_error_response(request, 500, 'Pipeline {property} not set.'.format(property=property_description))

    # TODO: add error if more than one location is returned
    processing_location = Location.objects.get(
        pipeline=sword_server.pipeline,
        purpose=Location.CURRENTLY_PROCESSING)

    destination_path = os.path.join(
        processing_location.full_path(),
        'watchedDirectories/activeTransfers/standardTransfer',
        os.path.basename(deposit.full_path()))

    # move to standard transfers directory
    destination_path = helpers.pad_destination_filepath_if_it_already_exists(destination_path)
    shutil.move(deposit.full_path(), destination_path)

    # wait to make sure the MCP responds to the directory being in the watch directory
    time.sleep(4)

    # make request to pipeline's transfer approval API
    data = urllib.urlencode({
        'username': sword_server.pipeline.api_username,
        'api_key': sword_server.pipeline.api_key,
        'directory': os.path.basename(destination_path),
        'type': 'standard' # TODO: make this customizable via a URL param
    })

    pipeline_endpoint_url = 'http://' + sword_server.pipeline.remote_name + '/api/transfer/approve/'
    approve_request = urllib2.Request(pipeline_endpoint_url, data)

    try:
        approve_response = urllib2.urlopen(approve_request)
    except:
        # move back to deposit directory
        shutil.move(destination_path, deposit.full_path())
        return {
            'error': True,
            'message': 'Request to pipeline ' + sword_server.pipeline.uuid + ' transfer approval API failed: check credentials and REST API IP whitelist.'
        } #_sword_error_response(request, 500, 'Request to pipeline transfer approval API failed: check credentials and REST API IP whitelist.')

    result = json.loads(approve_response.read())
    return result

"""
Example GET of files list:

  curl -v http://127.0.0.1:8000/api/v1/location/149cc29d-6472-4bcf-bee8-f8223bf60580/sword/media/

Example POST of file:

  curl -v -H "Content-Disposition: attachment; filename=joke.jpg" --request POST \
    --data-binary "@joke.jpg" \
    http://127.0.0.1:8000/api/v1/location/9c8b4ac0-0407-4360-a10d-af6c62a48b69/sword/media/

Example DELETE of all files:

  curl -v -XDELETE \
    http://127.0.0.1:8000/api/v1/location/9c8b4ac0-0407-4360-a10d-af6c62a48b69/sword/media/

Example DELETE of file:

  curl -v -XDELETE \
    http://127.0.0.1:8000/api/v1/location/9c8b4ac0-0407-4360-a10d-af6c62a48b69/sword/media/?filename=joke.jpg
"""
def deposit_media(request, uuid):
    deposit = helpers.get_deposit(uuid)

    if deposit == None:
        return _sword_error_response(request, 404, 'Deposit location {uuid} does not exist.'.format(uuid=uuid))

    if deposit.has_been_submitted_for_processing():
        return _sword_error_response(request, 400, 'This deposit has already been submitted for processing.')

    if request.method == 'GET':
        return HttpResponse(str(os.listdir(deposit.full_path())))
    elif request.method == 'PUT':
        # replace a file in the deposit
        return _handle_upload_request(request, deposit, True)
    elif request.method == 'POST':
        # add a file to the deposit
        return _handle_upload_request(request, deposit)
    elif request.method == 'DELETE':
        filename = request.GET.get('filename', '')
        if filename != '':
            file_path = os.path.join(deposit.full_path(), filename) 
            if os.path.exists(file_path):
                os.remove(file_path)
                return HttpResponse(status=204) # No content
            else:
                return _sword_error_response(request, 404, 'The path to this file (%s) does not exist.' % (file_path))
        else:
            for filename in os.listdir(deposit.full_path()):
                filepath = os.path.join(deposit.full_path(), filename)
                if os.path.isfile(filepath):
                    os.remove(filepath)
                elif os.path.isdir(filepath):
                    shutil.rmtree(filepath)

            return HttpResponse(status=204) # No content
    else:
        return _sword_error_response(request, 405, 'This endpoint only responds to the GET, POST, PUT, and DELETE HTTP methods.')

def _handle_upload_request(request, deposit, replace_file=False):
    if 'HTTP_CONTENT_DISPOSITION' in request.META:
        filename = helpers.parse_filename_from_content_disposition(request.META['HTTP_CONTENT_DISPOSITION']) 

        if filename != '':
            file_path = os.path.join(deposit.full_path(), filename)

            if replace_file:
                # if doing a file replace, the file being replaced must exist
                if os.path.exists(file_path):
                    return _handle_upload_request_with_potential_md5_checksum(
                        request,
                        file_path,
                        204
                    )
                else:
                    return _sword_error_response(request, 400, 'File does not exist.')
            else:
                # if adding a file, the file must not already exist
                if os.path.exists(file_path):
                    return _sword_error_response(request, 400, 'File already exists.')
                else:
                    return _handle_upload_request_with_potential_md5_checksum(
                        request,
                        file_path,
                        201
                    )
        else:
            return _sword_error_response(request, 400, 'No filename found in Content-disposition header.')
    else:
        return _sword_error_response(request, 400, 'Content-disposition must be set in request header.')

def _handle_upload_request_with_potential_md5_checksum(request, file_path, success_status_code):
    temp_filepath = helpers.write_request_body_to_temp_file(request)
    if 'HTTP_CONTENT_MD5' in request.META:
        md5sum = helpers.get_file_md5_checksum(temp_filepath)
        if request.META['HTTP_CONTENT_MD5'] != md5sum:
            os.remove(temp_filepath)
            return _sword_error_response(request, 400, 'MD5 checksum of uploaded file ({uploaded_md5sum}) does not match ' + 'checksum provided in header ({header_md5sum}).'.format(
                uploaded_md5sum=md5sum, header_md5sum=request.META['HTTP_CONTENT_MD5']))
        else:
            shutil.copyfile(temp_filepath, file_path)
            os.remove(temp_filepath)
            return HttpResponse(status=success_status_code)
    else:
        shutil.copyfile(temp_filepath, file_path)
        os.remove(temp_filepath)
        return HttpResponse(status=success_status_code)

"""
Example GET of state:

  curl -v http://localhost:8000/api/v1/location/96606387-cc70-4b09-b422-a7220606488d/sword/state/
"""
def deposit_state(request, uuid):
    deposit = helpers.get_deposit(uuid)

    if deposit == None:
        return _sword_error_response(request, 404, 'Deposit location {uuid} does not exist.'.format(uuid=uuid))

    if request.method == 'GET':
        state_term = deposit_downloading_status(uuid)
        state_description = 'Deposit initiation: ' + deposit_downloading_status(uuid)

        response = HttpResponse(render_to_string('locations/api/sword/state.xml', locals()))
        response['Content-Type'] = 'application/atom+xml;type=feed'
        return response
    else:
        return _sword_error_response(request, 405, 'This endpoint only responds to the GET HTTP method.')

# respond with SWORD 2.0 deposit receipt XML
def _deposit_receipt_response(request, deposit_uuid, status_code):
    deposit = helpers.get_deposit(deposit_uuid)

    # TODO: fix minor issues with template
    media_iri = request.build_absolute_uri(
        reverse('sword_deposit_media', kwargs={'api_name': 'v1',
            'resource_name': 'location', 'uuid': deposit_uuid}))

    edit_iri = request.build_absolute_uri(
        reverse('sword_deposit', kwargs={'api_name': 'v1',
            'resource_name': 'location', 'uuid': deposit_uuid}))

    state_iri = request.build_absolute_uri(
        reverse('sword_deposit_state', kwargs={'api_name': 'v1',
            'resource_name': 'location', 'uuid': deposit_uuid}))

    receipt_xml = render_to_string('locations/api/sword/deposit_receipt.xml', locals())

    response = HttpResponse(receipt_xml, mimetype='text/xml', status=status_code)
    response['Location'] = '/api/v1/location/' + deposit_uuid + '/sword/'
    return response

def _sword_error_response_render(request, error_details):
    error_details['request'] = request
    error_details['update_time'] = datetime.datetime.now().__str__()
    error_details['user_agent'] = request.META['HTTP_USER_AGENT']
    error_xml = render_to_string('locations/api/sword/error.xml', error_details)
    return HttpResponse(error_xml, status=error_details['status'])

def _sword_error_response(request, status, summary):
    error = _error(status, summary)
    return _sword_error_response_render(request, error)

def _error(status, summary):
    return {'summary': summary, 'status': status}
