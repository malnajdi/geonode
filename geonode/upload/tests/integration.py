# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""
See the README.rst in this directory for details on running these tests.
@todo allow using a database other than `development.db` - for some reason, a
      test db is not created when running using normal settings
@todo when using database settings, a test database is used and this makes it
      difficult for cleanup to track the layers created between runs
@todo only test_time seems to work correctly with database backend test settings
"""

from geonode.tests.base import GeoNodeLiveTestSupport

import os.path
from django.conf import settings
from django.db import connections

from geonode.maps.models import Map
from geonode.layers.models import Layer
from geonode.upload.models import Upload
from geonode.people.models import Profile
from geonode.documents.models import Document
from geonode.base.models import Link
from geonode.catalogue import get_catalogue
from geonode.tests.utils import upload_step, Client
from geonode.upload.utils import _ALLOW_TIME_STEP
from geonode.geoserver.helpers import ogc_server_settings, cascading_delete
from geonode.geoserver.signals import gs_catalog

from geoserver.catalog import Catalog
from gisdata import BAD_DATA
from gisdata import GOOD_DATA
from owslib.wms import WebMapService
from zipfile import ZipFile
from six import string_types

import re
import os
import csv
import glob
import time
from urllib.parse import unquote
from urllib.error import HTTPError
import logging
import tempfile
import unittest
import dj_database_url

GEONODE_USER = 'admin'
GEONODE_PASSWD = 'admin'
GEONODE_URL = settings.SITEURL.rstrip('/')
GEOSERVER_URL = ogc_server_settings.LOCATION
GEOSERVER_USER, GEOSERVER_PASSWD = ogc_server_settings.credentials

DB_HOST = settings.DATABASES['default']['HOST']
DB_PORT = settings.DATABASES['default']['PORT']
DB_NAME = settings.DATABASES['default']['NAME']
DB_USER = settings.DATABASES['default']['USER']
DB_PASSWORD = settings.DATABASES['default']['PASSWORD']
DATASTORE_URL = 'postgis://{}:{}@{}:{}/{}'.format(
    DB_USER,
    DB_PASSWORD,
    DB_HOST,
    DB_PORT,
    DB_NAME
)
postgis_db = dj_database_url.parse(DATASTORE_URL, conn_max_age=5)

logging.getLogger('south').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# create test user if needed, delete all layers and set password
u, created = Profile.objects.get_or_create(username=GEONODE_USER)
if created:
    u.first_name = "Jhònà"
    u.last_name = "çénü"
    u.set_password(GEONODE_PASSWD)
    u.save()
else:
    Layer.objects.filter(owner=u).delete()


def get_wms(version='1.1.1', type_name=None, username=None, password=None):
    """ Function to return an OWSLib WMS object """
    # right now owslib does not support auth for get caps
    # requests. Either we should roll our own or fix owslib
    if type_name:
        url = GEOSERVER_URL + \
            '%swms?request=getcapabilities' % type_name.replace(':', '/')
    else:
        url = GEOSERVER_URL + \
            'wms?request=getcapabilities'
    if username and password:
        return WebMapService(
            url,
            version=version,
            username=username,
            password=password
        )
    else:
        return WebMapService(url)


class UploaderBase(GeoNodeLiveTestSupport):

    type = 'layer'

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists('integration_settings.py'):
            os.unlink('integration_settings.py')

    def setUp(self):
        # await startup
        cl = Client(
            GEONODE_URL, GEONODE_USER, GEONODE_PASSWD
        )
        for i in range(10):
            time.sleep(.2)
            try:
                cl.get_html('/', debug=False)
                break
            except BaseException:
                pass

        self.client = Client(
            GEONODE_URL, GEONODE_USER, GEONODE_PASSWD
        )
        self.catalog = Catalog(
            GEOSERVER_URL + 'rest',
            GEOSERVER_USER,
            GEOSERVER_PASSWD,
            retries=ogc_server_settings.MAX_RETRIES,
            backoff_factor=ogc_server_settings.BACKOFF_FACTOR
        )

        settings.DATABASES['default']['NAME'] = DB_NAME

        connections['default'].settings_dict['ATOMIC_REQUESTS'] = False
        connections['default'].connect()

        self._tempfiles = []

    def _post_teardown(self):
        pass

    def tearDown(self):
        connections.databases['default']['ATOMIC_REQUESTS'] = False

        for temp_file in self._tempfiles:
            os.unlink(temp_file)

        # Cleanup
        Upload.objects.all().delete()
        Layer.objects.all().delete()
        Map.objects.all().delete()
        Document.objects.all().delete()

        if settings.OGC_SERVER['default'].get(
                "GEOFENCE_SECURITY_ENABLED", False):
            from geonode.security.utils import purge_geofence_all
            purge_geofence_all()

    def check_layer_geonode_page(self, path):
        """ Check that the final layer page render's correctly after
        an layer is uploaded """
        # the final url for uploader process. This does a redirect to
        # the final layer page in geonode
        resp, _ = self.client.get_html(path)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue('content-type' in resp.headers)

    def check_layer_geoserver_caps(self, type_name):
        """ Check that a layer shows up in GeoServer's get
        capabilities document """
        # using owslib
        wms = get_wms(
            type_name=type_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
        ws, layer_name = type_name.split(':')
        self.assertTrue(layer_name in wms.contents,
                        '%s is not in %s' % (layer_name, wms.contents))

    def check_layer_geoserver_rest(self, layer_name):
        """ Check that a layer shows up in GeoServer rest api after
        the uploader is done"""
        # using gsconfig to test the geoserver rest api.
        layer = self.catalog.get_layer(layer_name)
        self.assertIsNotNone(layer is not None)

    def check_and_pass_through_timestep(self, redirect_to):
        time_step = upload_step('time')
        srs_step = upload_step('srs')
        if srs_step in redirect_to:
            resp = self.client.make_request(redirect_to)
        else:
            self.assertTrue(time_step in redirect_to)
        resp = self.client.make_request(redirect_to)
        token = self.client.get_csrf_token(True)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.make_request(
            redirect_to, {'csrfmiddlewaretoken': token}, ajax=True)
        return resp, resp.json()

    def complete_raster_upload(self, file_path, resp, data):
        return self.complete_upload(file_path, resp, data, is_raster=True)

    def check_save_step(self, resp, data):
        """Verify the initial save step"""
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(isinstance(data, dict))
        # make that the upload returns a success True key
        self.assertTrue(data['success'], 'expected success but got %s' % data)
        self.assertTrue('redirect_to' in data)

    def complete_upload(self, file_path, resp, data, is_raster=False):
        """Method to check if a layer was correctly uploaded to the
        GeoNode.

        arguments: file path, the django http response

           Checks to see if a layer is configured in Django
           Checks to see if a layer is configured in GeoServer
               checks the Rest API
               checks the get cap document """

        layer_name, ext = os.path.splitext(os.path.basename(file_path))

        if not isinstance(data, string_types):
            self.check_save_step(resp, data)

            layer_page = self.finish_upload(
                data['redirect_to'],
                layer_name,
                is_raster)

            self.check_layer_complete(layer_page, layer_name)

    def finish_upload(
            self,
            current_step,
            layer_name,
            is_raster=False,
            skip_srs=False):
        if not is_raster and _ALLOW_TIME_STEP:
            resp, data = self.check_and_pass_through_timestep(current_step)
            self.assertEqual(resp.status_code, 200)
            if not isinstance(data, string_types):
                if data['success']:
                    self.assertTrue(
                        data['success'],
                        'expected success but got %s' %
                        data)
                    self.assertTrue('redirect_to' in data)
                    current_step = data['redirect_to']
                    self.wait_for_progress(data.get('progress'))

        if not is_raster and not skip_srs:
            self.assertTrue(upload_step('srs') in current_step)
            # if all is good, the srs step will redirect to the final page
            final_step = current_step.replace('srs', 'final')
            resp = self.client.make_request(final_step)
        else:
            self.assertTrue(upload_step('final') in current_step)
            resp = self.client.get(current_step)

        self.assertEqual(resp.status_code, 200)
        try:
            c = resp.json()
            url = c['url']
            url = unquote(url)
            # and the final page should redirect to the layer page
            # @todo - make the check match completely (endswith at least)
            # currently working around potential 'orphaned' db tables
            self.assertTrue(
                layer_name in url, 'expected %s in URL, got %s' %
                (layer_name, url))
            return url
        except BaseException:
            return current_step

    def check_upload_model(self, original_name):
        # we can only test this if we're using the same DB as the test instance
        if not settings.OGC_SERVER['default']['DATASTORE']:
            return
        upload = None
        try:
            upload = Upload.objects.filter(name__icontains=str(original_name)).last()
            # Making sure the Upload object is present on the DB and
            # the import session is COMPLETE
            if upload and not upload.complete:
                logger.warning(
                    "Upload not complete for Layer %s" %
                    original_name)
        except Upload.DoesNotExist:
            self.fail('expected to find Upload object for %s' % original_name)

    def check_layer_complete(self, layer_page, original_name):
        '''check everything to verify the layer is complete'''
        self.check_layer_geonode_page(layer_page)
        # @todo use the original_name
        # currently working around potential 'orphaned' db tables
        # this grabs the name from the url (it might contain a 0)
        type_name = os.path.basename(layer_page)
        layer_name = original_name
        try:
            layer_name = type_name.split(':')[1]
        except BaseException:
            pass

        # work around acl caching on geoserver side of things
        caps_found = False
        for i in range(10):
            time.sleep(.5)
            try:
                self.check_layer_geoserver_caps(type_name)
                caps_found = True
            except BaseException:
                pass
        if not caps_found:
            logger.warning(
                "Could not recognize Layer %s on GeoServer WMS Capa" %
                original_name)
        self.check_layer_geoserver_rest(layer_name)
        self.check_upload_model(layer_name)

    def check_invalid_projection(self, layer_name, resp, data):
        """ Makes sure that we got the correct response from an layer
        that can't be uploaded"""
        self.assertTrue(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.assertTrue(data['success'])
            srs_step = upload_step("srs")
            if "srs" in data['redirect_to']:
                self.assertTrue(srs_step in data['redirect_to'])
                resp, soup = self.client.get_html(data['redirect_to'])
                # grab an h2 and find the name there as part of a message saying it's
                # bad
                h2 = soup.find_all(['h2'])[0]
                self.assertTrue(str(h2).find(layer_name))

    def check_upload_complete(self, layer_name, resp, data):
        """ Makes sure that we got the correct response from an layer
        that can't be uploaded"""
        self.assertTrue(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.assertTrue(data['success'])
            final_step = upload_step("final")
            if "final" in data['redirect_to']:
                self.assertTrue(final_step in data['redirect_to'])

    def upload_folder_of_files(self, folder, final_check, session_ids=None):

        mains = ('.tif', '.shp', '.zip', '.asc')

        def is_main(_file):
            _, ext = os.path.splitext(_file)
            return (ext.lower() in mains)

        for main in filter(is_main, os.listdir(folder)):
            # get the abs path to the file
            _file = os.path.join(folder, main)
            base, _ = os.path.splitext(_file)
            resp, data = self.client.upload_file(_file)
            if session_ids is not None:
                if not isinstance(data, string_types) and data.get('url'):
                    session_id = re.search(
                        r'.*id=(\d+)', data.get('url')).group(1)
                    if session_id:
                        session_ids += [session_id]
            if not isinstance(data, string_types):
                self.wait_for_progress(data.get('progress'))
            final_check(base, resp, data)

    def upload_file(self, fname, final_check,
                    check_name=None, session_ids=None):
        if not check_name:
            check_name, _ = os.path.splitext(fname)
        resp, data = self.client.upload_file(fname)
        if session_ids is not None:
            if not isinstance(data, string_types):
                if data.get('url'):
                    session_id = re.search(
                        r'.*id=(\d+)', data.get('url')).group(1)
                    if session_id:
                        session_ids += [session_id]
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
        final_check(check_name, resp, data)

    def wait_for_progress(self, progress_url):
        if progress_url:
            resp = self.client.get(progress_url)
            assert resp.getcode() == 200, 'Invalid progress status code'
            json_data = resp.json()
            # "COMPLETE" state means done
            if json_data.get('state', '') == 'RUNNING':
                time.sleep(0.1)
                self.wait_for_progress(progress_url)

    def temp_file(self, ext):
        fd, abspath = tempfile.mkstemp(ext)
        self._tempfiles.append(abspath)
        return fd, abspath

    def make_csv(self, fieldnames, *rows):
        fd, abspath = self.temp_file('.csv')
        with open(abspath, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return abspath


class TestUpload(UploaderBase):

    def test_shp_upload(self):
        """ Tests if a vector layer can be uploaded to a running GeoNode/GeoServer"""
        layer_name = 'san_andres_y_providencia_water'
        fname = os.path.join(
            GOOD_DATA,
            'vector',
            '%s.shp' % layer_name)
        self.upload_file(fname,
                         self.complete_upload,
                         check_name='%s' % layer_name)

        test_layer = Layer.objects.filter(name__icontains='%s' % layer_name).last()
        if test_layer:
            layer_attributes = test_layer.attributes
            self.assertIsNotNone(layer_attributes)
            self.assertTrue(layer_attributes.count() > 0)

            # Links
            _def_link_types = ['original', 'metadata']
            _links = Link.objects.filter(link_type__in=_def_link_types)
            # Check 'original' and 'metadata' links exist
            self.assertIsNotNone(
                _links,
                "No 'original' and 'metadata' links have been found"
            )
            self.assertTrue(
                _links.count() > 0,
                "No 'original' and 'metadata' links have been found"
            )
            # Check original links in csw_anytext
            _post_migrate_links_orig = Link.objects.filter(
                resource=test_layer.resourcebase_ptr,
                resource_id=test_layer.resourcebase_ptr.id,
                link_type='original'
            )
            self.assertTrue(
                _post_migrate_links_orig.count() > 0,
                "No 'original' links has been found for the layer '{}'".format(
                    test_layer.alternate
                )
            )
            for _link_orig in _post_migrate_links_orig:
                self.assertIn(
                    _link_orig.url,
                    test_layer.csw_anytext,
                    "The link URL {0} is not present in the 'csw_anytext' attribute of the layer '{1}'".format(
                        _link_orig.url,
                        test_layer.alternate
                    )
                )
            # Check catalogue
            catalogue = get_catalogue()
            record = catalogue.get_record(test_layer.uuid)
            self.assertIsNotNone(record)
            self.assertTrue(
                hasattr(record, 'links'),
                "No records have been found in the catalogue for the resource '{}'".format(
                    test_layer.alternate
                )
            )
            # Check 'metadata' links for each record
            for mime, name, metadata_url in record.links['metadata']:
                try:
                    _post_migrate_link_meta = Link.objects.get(
                        resource=test_layer.resourcebase_ptr,
                        url=metadata_url,
                        name=name,
                        extension='xml',
                        mime=mime,
                        link_type='metadata'
                    )
                except Link.DoesNotExist:
                    _post_migrate_link_meta = None
                self.assertIsNotNone(
                    _post_migrate_link_meta,
                    "No '{}' links have been found in the catalogue for the resource '{}'".format(
                        name,
                        test_layer.alternate
                    )
                )

    def test_raster_upload(self):
        """ Tests if a raster layer can be upload to a running GeoNode GeoServer"""
        fname = os.path.join(GOOD_DATA, 'raster', 'relief_san_andres.tif')
        self.upload_file(fname, self.complete_raster_upload,
                         check_name='relief_san_andres')

        test_layer = Layer.objects.all().first()
        self.assertIsNotNone(test_layer)

    def test_zipped_upload(self):
        """Test uploading a zipped shapefile"""
        fd, abspath = self.temp_file('.zip')
        fp = os.fdopen(fd, 'wb')
        zf = ZipFile(fp, 'w')
        fpath = os.path.join(
            GOOD_DATA,
            'vector',
            'san_andres_y_providencia_poi.*')
        for f in glob.glob(fpath):
            zf.write(f, os.path.basename(f))
        zf.close()
        self.upload_file(abspath,
                         self.complete_upload,
                         check_name='san_andres_y_providencia_poi')

    def test_ascii_grid_upload(self):
        """ Tests the layers that ASCII grid files are uploaded along with aux"""
        session_ids = []

        PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
        thelayer_path = os.path.join(
            PROJECT_ROOT,
            'data/arc_sample')
        self.upload_folder_of_files(
            thelayer_path,
            self.complete_raster_upload,
            session_ids=session_ids)

    def test_invalid_layer_upload(self):
        """ Tests the layers that are invalid and should not be uploaded"""
        # this issue with this test is that the importer supports
        # shapefiles without an .prj
        session_ids = []

        invalid_path = os.path.join(BAD_DATA)
        self.upload_folder_of_files(
            invalid_path,
            self.check_invalid_projection,
            session_ids=session_ids)

    def test_coherent_importer_session(self):
        """ Tests that the upload computes correctly next session IDs"""
        session_ids = []

        # First of all lets upload a raster
        fname = os.path.join(GOOD_DATA, 'raster', 'relief_san_andres.tif')
        self.assertTrue(os.path.isfile(fname))
        self.upload_file(
            fname,
            self.complete_raster_upload,
            session_ids=session_ids)

        # Next force an invalid session
        invalid_path = os.path.join(BAD_DATA)
        self.upload_folder_of_files(
            invalid_path,
            self.check_invalid_projection,
            session_ids=session_ids)

        # Finally try to upload a good file anc check the session IDs
        fname = os.path.join(GOOD_DATA, 'raster', 'relief_san_andres.tif')
        self.upload_file(
            fname,
            self.complete_raster_upload,
            session_ids=session_ids)

        self.assertTrue(len(session_ids) >= 0)
        if len(session_ids) > 1:
            self.assertTrue(int(session_ids[0]) < int(session_ids[1]))

    def test_extension_not_implemented(self):
        """Verify a error message is return when an unsupported layer is
        uploaded"""

        # try to upload ourselves
        # a python file is unsupported
        unsupported_path = __file__
        if unsupported_path.endswith('.pyc'):
            unsupported_path = unsupported_path.rstrip('c')

        with self.assertRaises(HTTPError):
            self.client.upload_file(unsupported_path)

    def test_csv(self):
        '''make sure a csv upload fails gracefully/normally when not activated'''
        csv_file = self.make_csv(
            ['lat', 'lon', 'thing'], {'lat': -100, 'lon': -40, 'thing': 'foo'})
        layer_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.assertTrue('success' in data)
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], "/upload/csv")


@unittest.skipUnless(ogc_server_settings.datastore_db,
                     'Vector datastore not enabled')
class TestUploadDBDataStore(UploaderBase):

    def test_csv(self):
        """Override the baseclass test and verify a correct CSV upload"""

        csv_file = self.make_csv(
            ['lat', 'lon', 'thing'], {'lat': -100, 'lon': -40, 'thing': 'foo'})
        layer_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, form_data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(form_data, string_types):
            self.check_save_step(resp, form_data)
            csv_step = form_data['redirect_to']
            self.assertTrue(upload_step('csv') in csv_step)
            form_data = dict(
                lat='lat',
                lng='lon',
                csrfmiddlewaretoken=self.client.get_csrf_token())
            resp = self.client.make_request(csv_step, form_data)
            content = resp.json()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(content['status'], 'incomplete')

    def test_time(self):
        """Verify that uploading time based shapefile works properly"""
        cascading_delete(self.catalog, 'boxes_with_date')

        timedir = os.path.join(GOOD_DATA, 'time')
        layer_name = 'boxes_with_date'
        shp = os.path.join(timedir, '%s.shp' % layer_name)

        # get to time step
        resp, data = self.client.upload_file(shp)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], upload_step('time'))
            redirect_to = data['redirect_to']
            resp, data = self.client.get_html(upload_step('time'))
            self.assertEqual(resp.status_code, 200)
            data = dict(csrfmiddlewaretoken=self.client.get_csrf_token(),
                        time_attribute='date',
                        presentation_strategy='LIST',
                        )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js['success']:
                url = resp_js['redirect_to']

                resp = self.client.make_request(url, data)

                url = resp.json()['url']

                self.assertTrue(
                    url.endswith(layer_name),
                    'expected url to end with %s, but got %s' %
                    (layer_name,
                     url))
                self.assertEqual(resp.status_code, 200)

                url = unquote(url)
                self.check_layer_complete(url, layer_name)
                wms = get_wms(
                    type_name='geonode:%s' % layer_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                layer_info = list(wms.items())[0][1]
                self.assertEqual(100, len(layer_info.timepositions))
            else:
                self.assertTrue('error_msg' in resp_js)
                self.assertTrue(
                    'Source SRS is not valid' in resp_js['error_msg'])

    def test_configure_time(self):
        layer_name = 'boxes_with_end_date'
        # make sure it's not there (and configured)
        cascading_delete(gs_catalog, layer_name)

        def get_wms_timepositions():
            alternate_name = 'geonode:%s' % layer_name
            if alternate_name in get_wms().contents:
                metadata = get_wms().contents[alternate_name]
                self.assertTrue(metadata is not None)
                return metadata.timepositions
            else:
                return None

        thefile = os.path.join(
            GOOD_DATA, 'time', '%s.shp' % layer_name
        )
        resp, data = self.client.upload_file(thefile)

        # initial state is no positions or info
        self.assertTrue(get_wms_timepositions() is None)
        self.assertEqual(resp.status_code, 200)

        # enable using interval and single attribute
        if not isinstance(data, string_types):
            self.wait_for_progress(data.get('progress'))
            self.assertTrue(data['success'])
            self.assertTrue(data['redirect_to'], upload_step('time'))
            redirect_to = data['redirect_to']
            resp, data = self.client.get_html(upload_step('time'))
            self.assertEqual(resp.status_code, 200)
            data = dict(csrfmiddlewaretoken=self.client.get_csrf_token(),
                        time_attribute='date',
                        time_end_attribute='enddate',
                        presentation_strategy='LIST',
                        )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js['success']:
                url = resp_js['redirect_to']

                resp = self.client.make_request(url, data)

                url = resp.json()['url']

                self.assertTrue(
                    url.endswith(layer_name),
                    'expected url to end with %s, but got %s' %
                    (layer_name,
                     url))
                self.assertEqual(resp.status_code, 200)

                url = unquote(url)
                self.check_layer_complete(url, layer_name)
                wms = get_wms(
                    type_name='geonode:%s' % layer_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                layer_info = list(wms.items())[0][1]
                self.assertEqual(100, len(layer_info.timepositions))
            else:
                self.assertTrue('error_msg' in resp_js)
                self.assertTrue(
                    'Source SRS is not valid' in resp_js['error_msg'])
