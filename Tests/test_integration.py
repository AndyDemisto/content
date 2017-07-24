import time
from pprint import pformat
import uuid
import urllib
from test_utils import print_error

# ----- Constants ----- #
DEFAULT_TIMEOUT = 60
DEFAULT_INTERVAL = 10
ENTRY_TYPE_ERROR = 4


class PB_Status:
    COMPLETED = 'completed'
    FAILED = 'failed'
    IN_PROGRESS = 'inprogress'


# ----- Functions ----- #


# get integration configuration
def __get_integration_config(client, integration_name):
    res = client.req('POST', '/settings/integration/search', {
        'page': 0, 'size': 100, 'query': 'name:' + integration_name
    })

    res = res.json()
    all_configurations = res['configurations']
    match_configurations = [x for x in all_configurations if x['name'] == integration_name]

    if not match_configurations or len(match_configurations) == 0:
        print_error('integration was not found')
        return None

    return match_configurations[0]


# __test_integration_instance
def __test_integration_instance(client, module_instance):
    res = client.req('POST', '/settings/integration/test', module_instance)
    if res.status_code != 200:
        print_error('Integration-instance test ("Test" button) failed.\nBad status code: ' + str(res.status_code))
        return False

    result_object = res.json()
    success = result_object['success']
    if not success:
        print_error('Test integration failed.\n Failure message: ' + result_object['message'])
        return False

    return True


# return instance name if succeed, None otherwise
def __create_integration_instance(client, integration_name, integration_params):
    # get configuration config (used for later rest api
    configuration = __get_integration_config(client, integration_name)
    if not configuration:
        return None

    module_configuration = configuration['configuration']

    instance_name = integration_name + '_test' + str(uuid.uuid4())
    # define module instance
    module_instance = {
        'brand': configuration['name'],
        'category': configuration['category'],
        'configuration': configuration,
        'data': [],
        'enabled': "true",
        'engine': '',
        'id': '',
        'isIntegrationScript': True,
        'name': instance_name,
        'passwordProtected': False,
        'version': 0
    }

    # set module params
    for param_conf in module_configuration:
        if param_conf['display'] in integration_params or param_conf['name'] in integration_params:
            # param defined in conf

            key = param_conf['display'] if param_conf['display'] in integration_params else param_conf['name']
            if key == 'credentials':
                credentials = integration_params[key]
                param_value = {
                    'credential': '',
                    'identifier': credentials['identifier'],
                    'password': credentials['password'],
                    'passwordChanged': False
                }
            else:
                param_value = integration_params[key]

            param_conf['value'] = param_value
            param_conf['hasvalue'] = True
        elif param_conf['defaultValue']:
            # param is required - take default falue
            param_conf['value'] = param_conf['defaultValue']
        module_instance['data'].append(param_conf)
    res = client.req('PUT', '/settings/integration', module_instance)

    if res.status_code != 200:
        print_error('create instance failed with status code ' + str(res.status_code))
        print_error(pformat(res.json()))
        return None

    integration_config = res.json()
    module_instance['id'] = integration_config['id']

    # test integration
    test_succeed = __test_integration_instance(client, module_instance)

    if not test_succeed:
        return

    return module_instance['id']


# create incident with given name & playbook, and then fetch & return the incident
def __create_incident_with_playbook(client, name, playbook_id):
    # create incident
    kwargs = {'createInvestigation': True, 'playbookId': playbook_id}
    try:
        r = client.CreateIncident(name, None, None, None, None,
                         None, None, **kwargs)
    except RuntimeError as err:
        print_error(str(err))

    response_json = r.json()
    inc_id = response_json['id']

    # get incident
    incidents = client.SearchIncidents(0, 50, 'id:' + inc_id)

    # poll up to 1 second
    timeout = time.time() + 3
    while incidents['total'] != 1:
        incidents = client.SearchIncidents(0, 50, 'id:' + inc_id)
        if time.time() > timeout:
            print_error('failed to get incident with id:' + inc_id)
            return False

    return incidents['data'][0]


# returns current investigation playbook state - 'inprogress'/'failed'/'completed'
def __get_investigation_playbook_state(client, inv_id):
    res = client.req('GET', '/inv-playbook/' + inv_id, {})
    investigation_playbook = res.json()

    return investigation_playbook['state']


# return True if delete-incident succeeded, False otherwise
def __delete_incident(client, incident):
    res = client.req('POST', '/incident/batchDelete', {
        'ids': [incident['id']],
        'filter': {},
        'all': False
    })

    if res.status_code is not 200:
        print_error('delete incident failed\nStatus code' + str(res.status_code))
        print_error(pformat(res.json()))
        return False

    return True


# return True if delete-integration-instance succeeded, False otherwise
def __delete_integration_instance(client, instance_id):
    res = client.req('DELETE', '/settings/integration/' + urllib.quote(instance_id), {})
    if res.status_code is not 200:
        print_error('delete integration instance failed\nStatus code' + str(res.status_code))
        print_error(pformat(res.json()))
        return False
    return True


def __print_investigation_error(client, playbook_id, investigation_id):
    res = client.req('POST', '/investigation/' + urllib.quote(investigation_id), {})
    if res.status_code == 200:
        entries = res.json()['entries']
        print_error('Playbook ' + playbook_id + ' has failed:')
        for entry in entries:
            if entry['type'] == ENTRY_TYPE_ERROR:
                if entry['parentContent']:
                    print_error('\t- Command: ' + str(entry['parentContent']))
                print_error('\t- Body: ' + str(entry['contents']))


# 1. create integration instance
# 2. create incident with playbook
# 3. wait for playbook to finish run
# 4. if test pass - delete incident & instance
# return True if playbook completed successfully
def test_integration(client, integration_name, integration_params, playbook_id, skip_playbook, options={}):
    # create integration instance
    instance_id = __create_integration_instance(client, integration_name, integration_params)

    if not instance_id:
        print_error('Failed to create instance')
        return False

    print('Create integration succeed')
    # create incident with playbook

    if skip_playbook:
        print('Skip playbook')
        return True

    incident = __create_incident_with_playbook(client, integration_name, playbook_id)

    if not incident:
        return False

    investigation_id = incident['investigationId']
    if investigation_id is None or len(investigation_id) == 0:
        print_error('Failed to get investigation id of incident:' + incident)
        return False

    timeout_amount = options['timeout'] if 'timeout' in options else DEFAULT_TIMEOUT
    timeout = time.time() + timeout_amount
    interval = options['interval'] if 'interval' in options else DEFAULT_INTERVAL

    i = 1
    # wait for playbook to finish run
    while True:
        # give playbook time to run
        time.sleep(interval)

        # fetch status
        playbook_state = __get_investigation_playbook_state(client, investigation_id)

        if playbook_state == PB_Status.COMPLETED:
            break
        if playbook_state == PB_Status.FAILED:
            print_error(integration_name + ' failed with error/s')
            __print_investigation_error(client, playbook_id, investigation_id)
            break
        if time.time() > timeout:
            print_error(integration_name + ' failed on timeout')
            break

        print 'loop no.' + str(i) + ', playbook state is ' + playbook_state
        i = i + 1

    test_pass = playbook_state == PB_Status.COMPLETED
    if test_pass:
        # delete incident
        __delete_incident(client, incident)

        # delete integration instance
        __delete_integration_instance(client, instance_id)

    return test_pass