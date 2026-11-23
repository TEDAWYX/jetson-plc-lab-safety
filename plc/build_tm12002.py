from __future__ import print_function

import os
import sys

work_dir = os.path.dirname(os.path.abspath(__file__))
project_path = os.path.abspath(os.environ.get('LAB_SAFETY_PROJECT_PATH',
                                             os.path.join(work_dir, 'LabSafety-TM12002.project')))
with open(os.path.join(work_dir, 'LabSafetyAlarm.st'), 'r') as source_file:
    declaration, implementation = source_file.read().split('\nEND_VAR\n', 1)

declaration = declaration.replace('    wNanoFrame : WORD;',
    '    wNanoFrame AT %MW1000 : WORD;\n'
    '    wAlarmStatus AT %MW1001 : WORD;')
declaration = declaration.replace('    wResetRequest : WORD;', '    wResetRequest AT %MW1005 : WORD;')
declaration = declaration.replace('    wResetAck : WORD;', '    wResetAck AT %MW1006 : WORD;')
implementation += '''
// Read-back flags for Nano: four alarm latches, vision available, lamp, buzzer.
wAlarmStatus := WORD#16#0080 OR BOOL_TO_WORD(xLightAlarm)
    OR SHL(BOOL_TO_WORD(xHeatAlarm), 1)
    OR SHL(BOOL_TO_WORD(xIntrusionAlarm), 2)
    OR SHL(BOOL_TO_WORD(xBottleAlarm), 3)
    OR SHL(BOOL_TO_WORD(xVisionAvailable), 4)
    OR SHL(BOOL_TO_WORD(xAlarmLamp), 5)
    OR SHL(BOOL_TO_WORD(xAlarmBuzzer), 6);
IF xSoftwareReset THEN
    wResetAck := wResetRequest;
END_IF;
'''

project = projects.create(project_path)
try:
    project.add('Device', DeviceID(4096, '17d6 0001', '3.5.18.22'))
    controller = project.find('Device')[0]
    application = project.find('Application', True)[0]
    application.get_library_manager().add_library('Standard, 3.5.18.0 (System)')
    program = application.create_pou('PLC_PRG', PouType.Program)
    program.textual_declaration.replace(new_text=declaration + '\nEND_VAR\n')
    program.textual_implementation.replace(new_text=implementation.lstrip('\n'))

    config = application.create_task_configuration()
    task = config.create_task('MainTask')
    task.kind_of_task = KindOfTask.Cyclic
    task.interval = 'T#20ms'
    task.pous.add('PLC_PRG')
    print('Created TenLink application and cyclic task.')
    sys.stdout.flush()

    controller.add('IoDrvMasterBoard', DeviceID(40200, '1000 1200', '10.00.00.01'))
    board = controller.find('IoDrvMasterBoard')[0]
    board_parameters = next(c.host_parameters for c in board.connectors if c.connector_id == 2)
    channels = (
        (1000, 'in0', 'xLightInput'),
        (1001, 'in1', 'xHeatInput'),
        (1002, 'in2', 'xResetButton'),
        (2004, 'out4', 'xAlarmLamp'),
        (2005, 'out5', 'xAlarmBuzzer'),
    )
    for parameter_id, channel_name, variable_name in channels:
        mapping = board_parameters.by_id(parameter_id).io_mapping
        mapping.variable = 'Application.PLC_PRG.' + variable_name
        print('Mapped', channel_name, 'to', mapping.variable)

    controller.add('IoDrvModbusTCP', DeviceID(40205, '1000 1205', '10.00.00.00'))
    tcp = controller.find('IoDrvModbusTCP')[0]
    tcp_parameters = next(c.host_parameters for c in tcp.connectors if c.connector_id == 2)
    enable = tcp_parameters.by_id(3300)
    enable.value = '1'
    print('Modbus TCP enable:', enable.value, 'port:', tcp_parameters.by_id(3306).value,
          'memory base:', tcp_parameters.by_id(3307).value)
    sys.stdout.flush()

    project.save()
    application.generate_code()
    for category in system.get_message_categories():
        for message in system.get_message_objects(category):
            print(message.severity, message.text)
finally:
    project.close()
