# Copyright 2015 Intel Corporation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import logging
from oslo_config import cfg
from oslo_config import types
import oslo_messaging
from oslo_utils import encodeutils
import six

from searchlight.common import exception
import searchlight.elasticsearch
from searchlight.elasticsearch.plugins import utils
from searchlight.elasticsearch import ROLE_USER_FIELD
from searchlight import i18n
from searchlight import plugin


LOG = logging.getLogger(__name__)
_LW = i18n._LW
_LI = i18n._LI
_ = i18n._


indexer_opts = [
    cfg.StrOpt('index_name', default="searchlight",
               help="The default Elasticsearch index for plugins"),
    cfg.StrOpt('notifications_topic', default="searchlight_indexer",
               help="The default messaging notifications topic")
]

CONF = cfg.CONF
CONF.register_opts(indexer_opts, group='resource_plugin')


@six.add_metaclass(abc.ABCMeta)
class IndexBase(plugin.Plugin):
    NotificationHandlerCls = None

    def __init__(self):
        self.options = cfg.CONF[self.get_config_group_name()]

        self.engine = searchlight.elasticsearch.get_api()
        self.index_name = self.get_index_name()
        self.document_type = self.get_document_type()

        # This will be populated at load time.
        self.child_plugins = []
        self.parent_plugin = None

    @property
    def index_helper(self):
        if not getattr(self, '_index_helper', None):
            self._index_helper = utils.IndexingHelper(self)
        return self._index_helper

    @property
    def name(self):
        return "%s-%s" % (self.index_name, self.document_type)

    def initial_indexing(self, clear=True, setup_data=True):
        """Comprehensively install search engine index and put data into it."""
        if self.parent_plugin_type():
            LOG.debug(_LI(
                "Skipping initialization for %(doc_type)s; will be handled by"
                "parent (%(parent_type)s)") %
                {"doc_type": self.document_type,
                 "parent_type": self.parent_plugin_type()})
            return

        self.check_mapping_sort_fields()
        for child_plugin in self.child_plugins:
            child_plugin.check_mapping_sort_fields()

        if clear:
            # First delete the doc type
            self.clear_data()

        self.setup_index()
        self.setup_mapping()

        if setup_data:
            self.setup_data()

    def clear_data(self):
        self.engine.indices.delete_mapping(self.index_name,
                                           self.document_type,
                                           ignore=404)

        for child_plugin in self.child_plugins:
            self.engine.indices.delete_mapping(
                self.index_name,
                child_plugin.get_document_type(),
                ignore=404)

    def setup_index(self):
        """Create the index if it doesn't exist and update its settings."""
        index_exists = self.engine.indices.exists(self.index_name)
        if not index_exists:
            self.engine.indices.create(index=self.index_name)

        index_settings = self.get_settings()
        if index_settings:
            self.engine.indices.put_settings(index=self.index_name,
                                             body=index_settings)

        return index_exists

    def setup_mapping(self):
        """Update index document mapping."""
        # Using 'reversed' because in e-s 2.x, child mappings must precede
        # their parents, and the parent will be the first element
        for doc_type, mapping in self.get_full_mapping():
            self.engine.indices.put_mapping(index=self.index_name,
                                            doc_type=doc_type,
                                            body=mapping)

    def setup_data(self):
        """Insert all objects from database into search engine."""
        object_list = self.get_objects()
        documents = []
        for obj in object_list:
            document = self.serialize(obj)
            documents.append(document)

        self.index_helper.save_documents(documents)

        for child_plugin in self.child_plugins:
            child_plugin.setup_data()

    def get_facets(self, request_context, all_projects=False, limit_terms=0):
        """Get facets available for searching, in the form of a list of
        dicts with keys "name", "type" and optionally "options" if a field
        should have discreet allowed values
        """
        exclude_facets = self.facets_excluded
        is_admin = request_context.is_admin

        def include_facet(name):
            if name not in exclude_facets:
                return True

            if is_admin and exclude_facets[name]:
                return True

            return False

        def get_facets_for(mapping, prefix=''):
            facets = []
            for name, properties in six.iteritems(mapping):
                if properties.get('type') == 'nested':
                    if include_facet(prefix + name):
                        facets.extend(get_facets_for(properties['properties'],
                                                     "%s%s." % (prefix, name)))
                else:
                    indexed = properties.get('index', None) != 'no'
                    if indexed and include_facet(name):
                        facets.append({
                            'name': prefix + name,
                            'type': properties['type']
                        })
            return facets

        facets = get_facets_for(self.get_mapping()['properties'])

        # Don't retrieve facet terms for any excluded fields
        included_fields = set(f['name'] for f in facets)
        facet_terms_for = set(self.facets_with_options) & included_fields
        facet_terms = self._get_facet_terms(facet_terms_for,
                                            request_context,
                                            all_projects,
                                            limit_terms)
        for facet in facets:
            if facet['name'] in facet_terms:
                facet['options'] = facet_terms[facet['name']]

        return facets

    @property
    def facets_excluded(self):
        """A map of {name: allow_admin} that indicate which
        fields should not be offered as facet options.
        """
        return {}

    @property
    def facets_with_options(self):
        """An iterable of facet names that support facet options"""
        return ()

    def _get_facet_terms(self, fields, request_context,
                         all_projects, limit_terms):
        term_aggregations = utils.get_facets_query(fields, limit_terms)

        if term_aggregations:
            body = {
                'aggs': term_aggregations,
            }

            role_filter = request_context.user_role_filter
            plugin_filters = [{
                "term": {ROLE_USER_FIELD: role_filter}
            }]
            if not (request_context.is_admin and all_projects):
                plugin_filters.extend(
                    self._get_rbac_field_filters(request_context))

            body['query'] = {
                "filtered": {
                    "filter": {
                        "and": plugin_filters
                    }}}

            results = self.engine.search(
                index=self.get_index_name(),
                doc_type=self.get_document_type(),
                body=body,
                ignore_unavailable=True,
                search_type='count')

            agg_results = results.get('aggregations', {})
            facet_terms = utils.transform_facets_results(
                agg_results,
                self.get_document_type())

            if not agg_results:
                LOG.warning(_LW(
                    "No aggregations found for %(resource_type)s. There may "
                    "be a mapping problem.") %
                    {'resource_type': self.get_document_type()})
            return facet_terms
        return {}

    def check_mapping_sort_fields(self):
        """Check that fields that are expected to define a 'raw' field so so"""
        fields_needing_raw = searchlight.elasticsearch.RAW_SORT_FIELDS
        mapped_properties = self.get_mapping().get('properties', {})
        for field_name, field_mapping in six.iteritems(mapped_properties):
            if field_name in fields_needing_raw:
                raw = field_mapping.get('fields', {}).get('raw', None)
                if not raw:
                    msg_vals = {"field_name": field_name,
                                "index_name": self.get_index_name(),
                                "document_type": self.get_document_type()}
                    message = ("Field '%(field_name)s' for %(index_name)s/"
                               "%(document_type)s must contain a subfield "
                               "whose name is 'raw' for sorting." % msg_vals)
                    raise Exception(message)

    @abc.abstractmethod
    def get_objects(self):
        """Get list of all objects which will be indexed into search engine."""

    @abc.abstractmethod
    def serialize(self, obj):
        """Serialize database object into valid search engine document."""

    def get_document_id_field(self):
        """Whatever document field should be treated as the id. This field
        should also be mapped to _id in the elasticsearch mapping, though
        under role-based filtering, it will be modified to avoid clashes.
        """
        return "id"

    @classmethod
    def parent_plugin_type(cls):
        """If set, should be the resource type (canonical name) of
        the parent. Setting this for a plugin means that plugin cannot be
        initially indexed on its own, only as part of the parent indexing.
        """
        return None

    def get_parent_id_field(self):
        """Whatever field should be treated as the parent id. This is required
        for plugins with _parent definitions in their mappings. Documents to be
        indexed should contain this field.
        """
        return None

    def get_index_name(self):
        if self.options.index_name is not None:
            return self.options.index_name
        else:
            return cfg.CONF.resource_plugin.index_name

    @property
    def enabled(self):
        return self.options.enabled

    @classmethod
    def get_document_type(cls):
        """Get name of the document type.

        This is in the format of OS::Service::Resource typically.
        """
        raise NotImplemented()

    def register_parent(self, parent):
        if not self.parent_plugin:
            parent.child_plugins.append(self)
            self.parent_plugin = parent
            self.index_name = parent.index_name

    def get_index_display_name(self, indent_level=0):
        """The string used to list this plugin when indexing"""
        display = '\n' + '    ' * indent_level + '--> ' if indent_level else ''

        display += '%s (%s)' % (self.document_type, self.index_name)
        display += ''.join(c.get_index_display_name(indent_level + 1)
                           for c in self.child_plugins)
        return display

    def get_rbac_filter(self, request_context):
        """Get rbac filter as es json filter dsl. for non-admin queries."""
        # Add a document type filter to the plugin-specific fields
        plugin_filters = self._get_rbac_field_filters(request_context)
        document_type_filter = [{'type': {'value': self.get_document_type()}}]
        filter_fields = plugin_filters + document_type_filter

        return [
            {
                'indices': {
                    'index': self.get_index_name(),
                    'no_match_filter': 'none',
                    'filter': {
                        'and': filter_fields
                    }
                }
            }
        ]

    @abc.abstractmethod
    def _get_rbac_field_filters(self, request_context):
        """Return any RBAC field filters to be injected into an indices
        query. Document type will be added to this list.
        """
        return []

    def get_notification_handler(self):
        """Get the notification handler which implements NotificationBase."""
        if self.NotificationHandlerCls:
            return self.NotificationHandlerCls(self.index_helper,
                                               self.options)
        return None

    def filter_result(self, hit, request_context):
        """Filter each outgoing search result; document in hit['_source']. By
        default, this does nothing since information shouldn't be indexed.
        """
        pass

    def get_settings(self):
        """Get an index settings."""
        return {}

    def get_mapping(self):
        """Get an index mapping."""
        return {}

    def get_full_mapping(self):
        """Gets the full mapping doc for this type, including children. This
        returns a child-first (depth-first) generator.
        """
        # Assemble the children!
        for child_plugin in self.child_plugins:
            for plugin_type, mapping in child_plugin.get_full_mapping():
                yield plugin_type, mapping

        type_mapping = self.get_mapping()

        def apply_rbac_field(mapping):
            mapping['properties'][ROLE_USER_FIELD] = {
                'type': 'string',
                'index': 'not_analyzed',
                'include_in_all': False
            }

        apply_rbac_field(type_mapping)

        expected_parent_type = self.parent_plugin_type()
        mapping_parent = type_mapping.get('_parent', None)
        if mapping_parent:
            if mapping_parent['type'] != expected_parent_type:
                raise exception.IndexingException(
                    _("Mapping for '%(doc_type)s' contains a _parent "
                      "'%(actual)s' that doesn't match '%(expected)s'") %
                    {"doc_type": self.document_type,
                     "actual": mapping_parent['type'],
                     "expected": expected_parent_type})
        elif expected_parent_type:
            type_mapping['_parent'] = {'type': expected_parent_type}

        yield self.document_type, type_mapping

    @classmethod
    def get_plugin_type(cls):
        return "resource_plugin"

    @classmethod
    def get_plugin_name(cls):
        return cls.get_document_type().replace("::", "_").lower()

    @property
    def admin_only_fields(self):
        admin_only = self.options.admin_only_fields
        if not admin_only:
            return []
        return self.options.admin_only_fields.split(',')

    @property
    def requires_role_separation(self):
        return len(self.admin_only_fields) > 0

    @classmethod
    def get_plugin_opts(cls):
        opts = [
            cfg.StrOpt("index_name"),
            cfg.BoolOpt("enabled", default=True),
            cfg.StrOpt("admin_only_fields")
        ]
        if cls.NotificationHandlerCls:
            opts.extend(cls.NotificationHandlerCls.get_plugin_opts())
        return opts

    @classmethod
    def get_config_group_name(cls):
        """Override the get_plugin_name in order to use the document type as
        plugin name. This turns OS::Service::Resource to os_service_resource
        """
        config_name = cls.get_document_type().replace("::", "_").lower()
        return "resource_plugin:%s" % config_name


@six.add_metaclass(abc.ABCMeta)
class NotificationBase(object):

    def __init__(self, index_helper, options):
        self.index_helper = index_helper
        self.plugin_options = options

    def get_notification_supported_events(self):
        """Get the list of event types this plugin responds to."""
        return list(six.iterkeys(self.get_event_handlers()))

    @abc.abstractmethod
    def get_event_handlers(self):
        """Returns a mapping of event name to function"""

    @classmethod
    def _get_notification_exchanges(cls):
        """Return a list of oslo exchanges this plugin cares about"""

    @classmethod
    def get_plugin_opts(cls):
        opts = []
        exchanges = cls._get_notification_exchanges()
        if exchanges:
            defaults = " ".join("<notifications_topic>,%s" % i
                                for i in exchanges)
            opts.append(cfg.MultiOpt(
                'notifications_topics_exchanges',
                item_type=types.MultiString(),
                help='Override default topic,exchange pairs. '
                     'Defaults to %s' % defaults,
                default=[]))
        return opts

    def process(self, ctxt, publisher_id, event_type, payload, metadata):
        """Process the incoming notification message."""
        LOG.debug("Received %s event for %s",
                  event_type,
                  self.index_helper.document_type)
        try:
            self.get_event_handlers()[event_type](payload)
            return oslo_messaging.NotificationResult.HANDLED
        except Exception as e:

            LOG.error(encodeutils.exception_to_unicode(e))

    def get_notification_topics_exchanges(self):
        """"Returns a list of (topic,exchange), (topic,exchange)..)
        This will either be (CONF.notifications_topic,<exchange>) for
        each exchange in _get_notification_exchanges OR the list of
        values for CONF.plugin.notifications_topics_exchanges.
        """
        configured = self.plugin_options.notifications_topics_exchanges
        if configured:
            return [tuple(i.split(',')) for i in configured]
        else:
            return [(CONF.resource_plugin.notifications_topic, exchange)
                    for exchange in self._get_notification_exchanges()]
