# coding=utf-8
from __future__ import absolute_import
from octoprint.server import user_permission
from threading import Thread, Timer, Event
from time import time
import octoprint.plugin
import flask
import json
import math
import re

class LevelPCBPlugin(octoprint.plugin.SettingsPlugin,
                     octoprint.plugin.AssetPlugin,
                     octoprint.plugin.TemplatePlugin,
                     octoprint.plugin.SimpleApiPlugin,
                     octoprint.plugin.StartupPlugin):

    # globals
    status = 'IDLE'
    profile = dict()
    profiles = dict()
    position_absolute = True
    last_x = last_y = last_z = 0.0

    def on_after_startup(self):
        # load saved profiles from settings for fast access
        self.profiles = json.loads(self._settings.get(['profiles']))
        # save a reference to the selected profile for extra fast access
        self.profile = self.profiles[self._settings.get(['selected_profile'])]

    def get_settings_defaults(self):
        return dict(
            profiles = json.dumps(dict(disabled = dict(
                matrix = [],
                matrix_updated = 0.0,
                min_x = 0,
                min_y = 0,
                max_x = 200,
                max_y = 200,
                count_x = 5,
                count_y = 5,
                offset_x = 0,
                offset_y = 0,
                lift = 0,
                fade = 2,
                safe_homing = False,
                home_x = 100,
                home_y = 100
            ))),
            selected_profile = 'disabled',
            response_timeout = 20.0,
            debug = True
        )
    
    def get_api_commands(self):
        return dict(
            probe_start = [], probe_cancel = [], profile_changed = []
        )
    
    def on_api_command(self, command, data):
        if not user_permission.can():
            from flask import make_response
            return make_response('Insufficient permissions', 403)
        if command == 'probe_start':
            self.profiles = json.loads(self._settings.get(['profiles']))
            self.profile = self.profiles[self._settings.get(['selected_profile'])]
            self.set_status('PROBING', 'Probing finished, matrix saved')
            probe_thread = Thread(target = self.probe_start)
            probe_thread.start()
        elif command == 'probe_cancel':
            self.set_status('CANCEL', 'Probing cancelled, matrix not saved')
        elif command == 'profile_changed':
            self.profiles = json.loads(self._settings.get(['profiles']))
            self.profile = self.profiles[self._settings.get(['selected_profile'])]
        else:
            self._logger.info('Unknown command %s' % command)

    def probe_start(self):
        # calculate distance between probe points
        dist_x = (self.profile['max_x'] - self.profile['min_x']) / float(self.profile['count_x'] - 1)
        dist_y = (self.profile['max_y'] - self.profile['min_y']) / float(self.profile['count_y'] - 1)

        # probe points and add to matrix
        matrix = []
        for y in range(0, self.profile['count_y']):
            for x in range(0, self.profile['count_x']):
                # abort if status changed while executing the last loop (error occured or user clicked cancel)
                if self.status != 'PROBING':
                    return
                point = [self.profile['min_x'] + dist_x * x, self.profile['min_y'] + dist_y * y, 0.0]
                self.set_status('PROBING', 'Probing point %d of %d...' % (
                    y * self.profile['count_x'] + x + 1, self.profile['count_x'] * self.profile['count_y']
                ))
                # send G30 to execute Z probe at position
                cmd = ['G30 X%.3f Y%.3f' % (point[0], point[1])]
                if self._settings.get(['debug']):
                    # fake G30 response on virtual printer
                    cmd.append('!!DEBUG:send Bed X: %.3f Y: %.3f Z: %.3f' % (point[0], point[1], 0.5))
                response = self.send_command(
                    cmd, 'Bed X: ([0-9\.\-]+) Y: ([0-9\.\-]+) Z: ([0-9\.\-]+)'
                )
                if not response:
                    self.set_status('ERROR', 'Probing at location %.3f, %.3f timed out' % (point[0], point[1]))
                    return

                # extract result from regex match
                act_x, act_y, act_z = float(response.group(1)), float(response.group(2)), float(response.group(3))

                # compare the points we want to the actual position reported by the printer
                if not self.coords_equal(act_x, point[0]) or not self.coords_equal(act_y, point[1]):
                    self.set_status('ERROR',
                        'Probing failed: Coordinates mismatch, expected %.3f, %.3f, got %.3f, %.3f' %
                        (point[0], point[1], act_x, act_y)
                    )
                    return
                
                # write z offset into matrix
                point[2] = act_z

                # send probe result to front-end
                self.send_point(point)
                matrix.append(point)
        
        # matrix is now populated, save in settings
        self.profile['matrix'] = matrix
        self.profile['matrix_updated'] = time()
        self._settings.set(['profiles'], json.dumps(self.profiles))
        self._settings.save()

        # notify front-end with new data and status
        self.send_profile(self.profile)
        self.set_status('IDLE', 'Probing finished')

    # sends a command to the printer and waits for the specified response
    command_event = command_regex = command_match = None
    def send_command(self, command, responseRegex):
        self.command_event = Event()
        self.command_regex = responseRegex
        self._printer.commands(command)
        result = self.command_event.wait(self._settings.get(['response_timeout']))
        if result:
            return self.command_match
        else:
            return None
    
    def on_gcode_received(self, comm, line, *args, **kwargs):
        if self.command_regex:
            self.command_match = re.search(self.command_regex, line)
            if self.command_match:
                self.command_regex = None
                self.command_event.set()
        return line

    def on_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        # remove comment from command if any
        index = cmd.find(';')
        if index != -1:
            cmd = cmd[:index]
        # evaluate and change commands
        if gcode and gcode == 'G90':
            self.position_absolute = True
        elif gcode and gcode == 'G91':
            self.position_absolute = False
        elif gcode and gcode in ('G0', 'G00', 'G1', 'G01'):
            if len(self.profile['matrix']) == 0:
                # we have no matrix, do nothing
                return cmd
            # calculate z-offset at given position
            # first get X/Y/Z-coordinates from command
            match_x = re.search('X([\-\d\.]+)', cmd, re.IGNORECASE)
            match_y = re.search('Y([\-\d\.]+)', cmd, re.IGNORECASE)
            match_z = re.search('Z([\-\d\.]+)', cmd, re.IGNORECASE)
            x = y = z = 0.0
            if match_x:
                x = float(match_x.group(1))
            else:
                x = self.last_x
            if match_y:
                y = float(match_y.group(1))
            else:
                y = self.last_y
            # calculate surrounding matrix points
            dist_x = (self.profile['max_x'] - self.profile['min_x']) / float(self.profile['count_x'] - 1)
            dist_y = (self.profile['max_y'] - self.profile['min_y']) / float(self.profile['count_y'] - 1)
            index_x = (x - self.profile['min_x']) / dist_x
            index_y = (y - self.profile['min_y']) / dist_y
            # find out where the point is relative to the matrix
            nearby = []
            if x < self.profile['min_x']:
                if y < self.profile['min_y']:
                    # point is top left of matrix
                    nearby.append([0, 0])
                elif y > self.profile['max_y']:
                    # point is bottom left of matrix
                    nearby.append([0, self.profile['count_y'] - 1])
                else:
                    # point is left of matrix
                    nearby.append([0, math.floor(index_y)])
                    nearby.append([0, math.ceil(index_y)])
            elif x > self.profile['max_x']:
                if y < self.profile['min_y']:
                    # point is top right of matrix
                    nearby.append([self.profile['count_x'] - 1, 0])
                elif y > self.profile['max_y']:
                    # point is bottom right of matrix
                    nearby.append([self.profile['count_x'] - 1, self.profile['count_y'] - 1])
                else:
                    # point is right of matrix
                    nearby.append([self.profile['count_x'] - 1, math.floor(index_y)])
                    nearby.append([self.profile['count_x'] - 1, math.ceil(index_y)])
            else:
                if y < self.profile['min_y']:
                    # point is top of matrix
                    nearby.append([math.floor(index_x), 0])
                    nearby.append([math.ceil(index_x), 0])
                elif y > self.profile['max_y']:
                    # point is bottom of matrix
                    nearby.append([math.floor(index_x), self.profile['count_y'] - 1])
                    nearby.append([math.ceil(index_x), self.profile['count_y'] - 1])
                else:
                    # point is inside matrix, use all 4 nearby points
                    nearby = [
                        [ math.floor(index_x), math.floor(index_y) ],
                        [ math.ceil(index_x),  math.floor(index_y) ],
                        [ math.floor(index_x), math.ceil(index_y)  ],
                        [ math.ceil(index_x),  math.ceil(index_y)  ]
                    ]
            
            self._logger.info('++++')
            for n in nearby:
                self._logger.info(self.profile['matrix'][int(n[1]) * int(self.profile['count_x']) + int(n[0])])
            self._logger.info('----')

            # store last X/Y
            self.last_x = x
            self.last_y = y

            # insert z-offset
            if match_z:
                self.last_z = float(match_z.group(1))
                return cmd[:match_z.start()] + 'Z%.3f' % 1.234 + cmd[match_z.end():]
            else:
                return '%s Z%.3f' % (cmd, 1.234 + self.last_z)
        elif gcode and gcode == 'G28' and 'z' in cmd.lower() and self.profile['safe_homing']:
            commands = []
            # lift carriage if setting is positive, respecting current positioning mode
            if self.profile['lift'] > 0:
                lift = 'G0 Z%.3f' % self.profile['lift']
                if self.position_absolute:
                    commands.extend(['G91', 'G0 Z%.3f' % self.profile['lift'], 'G90'])
                else:
                    commands.append(lift)
            # prepend movement command to homing command
            commands.extend([
                'G0 X%.3f Y%.3f' % (
                    self.profile['home_x'] + self.profile['offset_x'],
                    self.profile['home_y'] + self.profile['offset_x']
                ),
                cmd
            ])
            # we don't know where the printer moves to, clear last coordinates
            self.last_x = self.last_y = self.last_z = 0.0
            return commands
        elif gcode and gcode == 'G30' and self.profile['lift'] > 0:
            return ['G91', 'G0 Z%.3f' % self.profile['lift'], 'G90', cmd]

    # set the status variable and send change to front-end
    def set_status(self, status, text):
        self.status = status
        self._plugin_manager.send_plugin_message(self._identifier, dict(status = status, text = text))
    
    # send a measured point to the UI
    def send_point(self, point):
        self._plugin_manager.send_plugin_message(
            self._identifier,
            dict(point = point)
        )

    def send_profile(self, profile):
        self._plugin_manager.send_plugin_message(self._identifier, dict(profile = profile))
    
    # compares two coordinates for equality with 0.1mm tolerance
    def coords_equal(self, float1, float2):
        return abs(float1 - float2) < 0.1

    def get_assets(self):
        return dict(
            js = ['js/levelpcb.js'],
            css = ['css/levelpcb.css']
        )

    def get_template_configs(self):
        return [
            dict(type = 'navbar', custom_bindings=False),
            dict(type = 'settings', custom_bindings=False)
        ]

    def get_update_information(self):
        return dict(
            levelpcb=dict(
                displayName='LevelPCB',
                displayVersion=self._plugin_version,

                # version check: github repository
                type='github_release',
                user='TazerReloaded',
                repo='OctoPrint-LevelPCB',
                current=self._plugin_version,

                # update method: pip
                pip='https://github.com/TazerReloaded/OctoPrint-LevelPCB/archive/{target_version}.zip'
            )
        )

__plugin_name__ = 'LevelPCB'

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = LevelPCBPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        'octoprint.plugin.softwareupdate.check_config': __plugin_implementation__.get_update_information,
        'octoprint.comm.protocol.gcode.received': __plugin_implementation__.on_gcode_received,
        'octoprint.comm.protocol.gcode.queuing': __plugin_implementation__.on_gcode_queuing,
    }
