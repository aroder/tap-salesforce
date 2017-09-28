#!/usr/bin/env python3
import json
import re
from tap_salesforce.salesforce import (Salesforce, sf_type_to_json_schema, TapSalesforceException)

import singer
import singer.metrics as metrics
import singer.schema
from singer import utils
from singer import (transform,
                    UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
                    Transformer, _transform_datetime)

LOGGER = singer.get_logger()

REQUIRED_CONFIG_KEYS = ['refresh_token', 'token', 'client_id', 'client_secret', 'start_date']
CONFIG = {
    'refresh_token': None,
    'token': None,
    'client_id': None,
    'client_secret': None,
    'start_date': None
}

def get_replication_key(sobject_name, fields):
    fields_list = [f['name'] for f in fields]

    if 'SystemModstamp' in fields_list:
        return 'SystemModstamp'
    elif 'LastModifiedDate' in fields_list:
        return 'LastModifiedDate'
    elif 'CreatedDate' in fields_list:
        return 'CreatedDate'
    elif  'LoginTime' in fields_list and sobject_name == 'LoginHistory':
        return 'LoginTime'
    else:
        return None

def build_state(raw_state, catalog):
    state = {}

    for catalog_entry in catalog['streams']:
        tap_stream_id = catalog_entry['tap_stream_id']
        replication_key = catalog_entry['replication_key']

        if catalog_entry['schema']['selected'] and replication_key:
            replication_key_value = singer.get_bookmark(raw_state,
                                                        tap_stream_id,
                                                        replication_key)

            if replication_key_value:
                state = singer.write_bookmark(state,
                                              tap_stream_id,
                                              replication_key,
                                              replication_key_value)
            else:
                state = singer.write_bookmark(state,
                                              tap_stream_id,
                                              replication_key,
                                              CONFIG['start_date'])
    return state

def create_property_schema(field):
    if field['name'] == "Id":
        inclusion = "automatic"
    else:
        inclusion = "available"

    result = {
        'inclusion': inclusion,
        'selected': False,
        'type': sf_type_to_json_schema(field['type'], field['nillable'])
    }
    return (result, field['compoundFieldName'])

# dumps a catalog to stdout
def do_discover(salesforce):
    # describe all
    global_description = salesforce.describe()

    # for each SF Object describe it, loop its fields and build a schema
    entries = []
    for sobject in global_description['sobjects']:
        sobject_name = sobject['name']
        sobject_description = salesforce.describe(sobject_name)

        #TODO - put this in its own function
        match = re.search('^api-usage=(\d+)/(\d+)$', salesforce.rate_limit)

        if match:
            LOGGER.info("Used {} of {} daily API quota".format(match.group(1), match.group(2)))

        fields = sobject_description['fields']
        replication_key = get_replication_key(sobject_name, fields)

        compound_fields = set()
        properties = {}

        for f in fields:
            property_schema, compound_field_name = create_property_schema(f)

            if compound_field_name:
                compound_fields.add(compound_field_name)

            properties[f['name']] = property_schema

        if replication_key:
            properties[replication_key]['inclusion'] = "automatic"

        if len(compound_fields) > 0:
            LOGGER.info("Not syncing the following compound fields for object {}: {}".format(
                sobject_name,
                ', '.join(sorted(compound_fields))))

        schema = {
            'type': 'object',
            'additionalProperties': False,
            'selected': False,
            'properties': {k:v for k,v in properties.items() if k not in compound_fields}
        }

        entry = {
            'stream': sobject_name,
            'tap_stream_id': sobject_name,
            'schema': schema,
            'replication_key': replication_key
        }

        entries.append(entry)

    return {'streams': entries}

def transform_data_hook(data, typ, schema):
    # TODO:
    # remove attributes field
    # rename table: prefix with "sf_ and replace "__" with "_" (this is probably just stream aliasing used for transmuted legacy connections)
    # filter out nil PKs
    # filter out of bounds updated at values?
    return data

def do_sync(salesforce, catalog, state):
    # TODO: Before bulk query:
    # filter out unqueryables
          # "Announcement"
          # "ApexPage"
          # "CollaborationGroupRecord"
          # "ContentDocument"
          # "ContentDocumentLink"
          # "FeedItem"
          # "FieldDefinition"
          # "IdeaComment"
          # "ListViewChartInstance"
          # "Order"
          # "PlatformAction"
          # "TopicAssignment"
          # "UserRecordAccess"
          # "Attachment" ; Contains large BLOBs that IAPI v2 can't handle.
          # "DcSocialProfile"
          # ; These Datacloud* objects don't support updated-at
          # "DatacloudCompany"
          # "DatacloudContact"
          # "DatacloudOwnedEntity"
          # "DatacloudPurchaseUsage"
          # "DatacloudSocialHandle"
          # "Vote"

          # ActivityHistory
          # EmailStatus

    # Bulk Data Query
    selected_catalog_entries = [e for e in catalog['streams'] if e['schema']['selected']]

    for catalog_entry in selected_catalog_entries:
        with Transformer(pre_hook=transform_data_hook) as transformer:
             with metrics.record_counter(catalog_entry['stream']) as counter:
                 replication_key = catalog_entry['replication_key']

                 for rec in salesforce.bulk_query(catalog_entry, state):
                     counter.increment()
                     record = transformer.transform(rec, catalog_entry['schema'])

                     singer.write_record(catalog_entry['stream'], record, catalog_entry.get('stream_alias', None))

                     if replication_key:
                         singer.write_bookmark(state,
                                               catalog_entry['tap_stream_id'],
                                               replication_key,
                                               record[replication_key])

                         singer.write_state(state)

def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    CONFIG.update(args.config)

    sf = Salesforce(refresh_token=CONFIG['refresh_token'],
                    sf_client_id=CONFIG['client_id'],
                    sf_client_secret=CONFIG['client_secret'])
    sf.login()

    if args.discover:
        with open("/tmp/catalog.json", 'w') as f:
            f.write(json.dumps(do_discover(sf)))
    elif args.properties:
        state = build_state(args.state, args.properties)

        do_sync(sf, args.properties, state)

def main():
    try:
        main_impl()
    except TapSalesforceException as e:
        LOGGER.critical(e)
        sys.exit(1)
    except Exception as e:
        LOGGER.critical(e)
        raise e
