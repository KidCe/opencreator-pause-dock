"""Docked pause/resume for OpenCreator. Physical acceptance testing is pending."""
import logging
import math


class Creator5PauseDock:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.park_x = config.getfloat('park_x', 125.)
        self.park_y = config.getfloat('park_y', 220.)
        self.hop = config.getfloat('z_hop', 5., above=0.)
        self.z_speed = config.getfloat('z_speed', 10., above=0.)
        self.pause_timeout = config.getfloat('pause_timeout', 36000., above=0.)
        self.session = None
        self.phase = 'idle'
        self.hooks_installed = False
        self.original_cancel = None
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        for name, handler in [('C5_PAUSE_DOCK', self.cmd_pause),
                              ('C5_RESUME_DOCK', self.cmd_resume),
                              ('C5_PAUSE_DOCK_RESET', self.cmd_reset)]:
            self.gcode.register_command(name, handler)

    def _handle_ready(self):
        if self.hooks_installed:
            return
        # Macros finish their rename_existing work during klippy:connect.
        # Hook afterwards, keeping OpenCreator's entire current cancel routine.
        if self.printer.lookup_object('creator5_pause_dock', None) is not None:
            raise self.printer.config_error(
                'Remove the legacy pause-dock accessory before enabling this plugin')
        for name, alias in (('PAUSE', 'PAUSE_BASE'), ('RESUME', 'RESUME_BASE')):
            macro = self.printer.lookup_object('gcode_macro ' + name, None)
            if macro is None or macro.rename_existing != alias:
                raise self.printer.config_error(
                    'Docked pause requires OpenCreator %s with rename_existing: %s'
                    % (name, alias))
        required = ('PAUSE', 'RESUME', 'CANCEL_PRINT', 'PAUSE_BASE', 'RESUME_BASE',
                    'C5_TOOL_DOCK', 'C5_TOOL_SELECT')
        commands = self.gcode.get_status(0.)['commands']
        if any(name not in commands for name in required):
            raise self.printer.config_error('Docked pause requires OpenCreator tool commands')
        if not self.printer.lookup_object('creator5_toolchanger', None):
            raise self.printer.config_error('Docked pause requires Creator5Toolchanger')
        replacements = [('PAUSE', self.cmd_pause), ('RESUME', self.cmd_resume),
                        ('CANCEL_PRINT', self.cmd_cancel)]
        # Roll back registration if any replacement fails. Never leave half a hook set.
        originals = []
        try:
            for name, handler in replacements:
                original = self.gcode.register_command(name, None)
                originals.append((name, original))
                self.gcode.register_command(name, handler)
        except Exception:
            for name, original in reversed(originals):
                self.gcode.register_command(name, None)
                self.gcode.register_command(name, original)
            raise
        self.original_cancel = originals[-1][1]
        self.hooks_installed = True

    def cmd_cancel(self, gcmd):
        try:
            self.cmd_reset(gcmd)
        except Exception:
            # A timeout-restore error must never swallow the actual cancel operation.
            logging.exception('Docked pause cleanup failed during cancellation')
            self.session = None
            self.phase = 'idle'
        return self.original_cancel(gcmd)

    def _run(self, script):
        self.gcode.run_script_from_command(script)

    def _status(self, name):
        return self.printer.lookup_object(name).get_status(
            self.printer.get_reactor().monotonic())

    def _check_homed(self, gcmd):
        status = self._status('toolhead')
        if any(axis not in status['homed_axes'] for axis in 'xyz'):
            raise gcmd.error('XYZ must remain homed; automatic re-homing is forbidden')
        return status

    def _move(self, coordinates, speed):
        # Physical carriage coordinates, as in Creator5Toolchanger._move().
        # Bypass nozzle offsets, G92, M220 and mesh for clearance/parking.
        try:
            self.printer.lookup_object('toolhead').manual_move(coordinates, speed)
        finally:
            self.printer.lookup_object('gcode_move').reset_last_position()

    def _clearance(self, gcmd, original_z, hop, safe_z):
        status = self._check_homed(gcmd)
        maximum = status['axis_maximum'][2]
        target = max(original_z + hop, safe_z, status['position'][2])
        if not math.isfinite(target) or target > maximum:
            raise gcmd.error('Not enough Z travel for the full pause clearance')
        self._move([None, None, target], self.z_speed)

    def _lane(self, gcmd, tc, tool):
        afc = self.printer.lookup_object('AFC')
        name = 'extruder' if tool == 0 else 'extruder%d' % tool
        # Match the mapping used by the installed Creator5Toolchanger.
        lane = tc._afc_lane(afc, name)
        if (lane is None or afc.in_toolchange or afc.error_state
                or lane.extruder_obj.creator5_tool_index != tool):
            raise gcmd.error('AFC lane is unavailable, in error, or changing tools')
        return afc, lane, name

    def cmd_pause(self, gcmd):
        pause = self.printer.lookup_object('pause_resume')
        if pause.is_paused:
            gcmd.respond_info('Already paused; saved print position retained')
            return
        # Stop the stream and capture PAUSE_STATE before any auxiliary move.
        self._run('PAUSE_BASE')
        self.session = None
        self.phase = 'preparing'
        try:
            tc = self.printer.lookup_object('creator5_toolchanger')
            status = self._check_homed(gcmd)
            tool = tc.attached_tool_from_pins()
            if tool is None or tc.busy:
                raise gcmd.error('An attached, idle toolchanger is required')
            afc, lane, heater_name = self._lane(gcmd, tc, tool)
            if afc.current != lane.name or lane.extruder_obj.lane_loaded != lane.name:
                raise gcmd.error('Physical tool and AFC loaded lane do not match')
            target = self._status(heater_name)['target']
            if not math.isfinite(target) or target <= 0.:
                raise gcmd.error('The printing tool has no positive temperature target')
            hop = gcmd.get_float('Z_HOP', self.hop, above=0.)
            if not all(math.isfinite(v) for v in (hop, self.park_x, self.park_y)):
                raise gcmd.error('Pause coordinates must be finite')
            for axis, value in enumerate((self.park_x, self.park_y)):
                if not status['axis_minimum'][axis] <= value <= status['axis_maximum'][axis]:
                    raise gcmd.error('Pause park position is outside machine limits')
            if self.park_x >= tc.approach_x:
                raise gcmd.error('Pause park X must be inboard of the dock corridor')
            old_timeout = self._status('idle_timeout')['idle_timeout']
            self.session = dict(tool=tool, heater=heater_name, target=target,
                                position=list(status['position'][:3]), hop=hop,
                                timeout=old_timeout, lane=lane.name,
                                accel=status['max_accel'],
                                mcr=status.get('minimum_cruise_ratio'),
                                filename=self._status('print_stats')['filename'])
            self._run('SET_IDLE_TIMEOUT TIMEOUT=%.3f' % max(old_timeout, self.pause_timeout))
            self._clearance(gcmd, self.session['position'][2], hop, tc.safe_z)
            self._run('C5_TOOL_DOCK')
            if tc.attached_tool_from_pins() is not None:
                raise gcmd.error('Tool did not dock; parking is blocked')
            # Cooling starts only after the existing dock routine succeeds.
            self._run('SET_HEATER_TEMPERATURE HEATER=%s TARGET=0' % heater_name)
            self._move([self.park_x, self.park_y, None], tc.clear_travel_speed)
            self._run('M400')
            self.phase = 'parked'
        except Exception:
            self.phase = 'failed'
            raise

    def cmd_resume(self, gcmd):
        pause = self.printer.lookup_object('pause_resume')
        if not pause.is_paused:
            gcmd.respond_info('Print is not paused')
            return
        if self.phase != 'parked' or self.session is None:
            raise gcmd.error('No completed dock pause; recovery or cancel is required')
        s = self.session
        try:
            tc = self.printer.lookup_object('creator5_toolchanger')
            self._check_homed(gcmd)
            if self._status('print_stats')['filename'] != s['filename']:
                raise gcmd.error('Print job changed during pause')
            afc, lane, _ = self._lane(gcmd, tc, s['tool'])
            if tc.busy or tc.attached_tool_from_pins() is not None:
                raise gcmd.error('Keep the carriage empty until automatic pickup')
            velocity = gcmd.get_float('VELOCITY', tc.clear_travel_speed, above=0.)
            if not math.isfinite(velocity):
                raise gcmd.error('Resume velocity must be finite')
            velocity = min(velocity, tc.clear_travel_speed,
                           self._status('toolhead')['max_velocity'])
            self.phase = 'reheating'
            # The hotend stays in its protected dock for the entire wait.
            self._run('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.3f' % (s['heater'], s['target']))
            self._run('TEMPERATURE_WAIT SENSOR=%s MINIMUM=%.3f MAXIMUM=%.3f' %
                      (s['heater'], s['target'] - 2., s['target'] + 2.))
            if not pause.is_paused or self.session is not s or self.phase != 'reheating':
                raise gcmd.error('Pause was cleared or cancelled while heating')
            # A manual pause jog may have reduced the available clearance.
            self._clearance(gcmd, s['position'][2], s['hop'], tc.safe_z)
            self.phase = 'picking_up'
            self._run('C5_TOOL_SELECT T=%d' % s['tool'])
            if tc.attached_tool_from_pins() != s['tool']:
                raise gcmd.error('Saved tool is not physically attached')
            # C5_TOOL_DOCK already calls set_tool_unloaded(). Restore the same
            # AFC bookkeeping through its lane helper, without a second trip,
            # filament loading/purge or AFC's independent saved-position return.
            lane.set_tool_loaded(normal_toolchange=True)
            if afc.current != lane.name or lane.extruder_obj.lane_loaded != lane.name:
                raise gcmd.error('AFC did not confirm the resumed tool')
            self.phase = 'returning'
            # Restore the original coordinate/extrusion frame without motion.
            self._run('RESTORE_GCODE_STATE NAME=PAUSE_STATE MOVE=0')
            self._run('SET_VELOCITY_LIMIT ACCEL=%.3f' % tc.pickup_accel)
            if tc.pickup_minimum_cruise_ratio is not None and s['mcr'] is not None:
                self._run('SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%.6f' % tc.pickup_minimum_cruise_ratio)
            self._move([s['position'][0], s['position'][1], None], velocity)
            self._run('M400')
            # We are now above the original XY. Base resume performs only the
            # final Z return, restores PAUSE_STATE and restarts the stream.
            self._run('SET_IDLE_TIMEOUT TIMEOUT=%.3f' % s['timeout'])
            self._restore_limits(s)
            self._run('RESUME_BASE VELOCITY=%.3f' % self.z_speed)
            self.session = None
            self.phase = 'idle'
        except Exception:
            self.phase = 'failed'
            try:
                self._restore_limits(s)
            except Exception:
                logging.exception('Could not restore pause motion limits')
            raise

    def _restore_limits(self, session):
        self._run('SET_VELOCITY_LIMIT ACCEL=%.3f' % session['accel'])
        if session['mcr'] is not None:
            self._run('SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%.6f' % session['mcr'])

    def cmd_reset(self, gcmd):
        if self.session is not None:
            self._run('SET_IDLE_TIMEOUT TIMEOUT=%.3f' % self.session['timeout'])
        self.session = None
        self.phase = 'idle'

    def get_status(self, eventtime):
        return {'phase': self.phase,
                'hooks_installed': self.hooks_installed,
                'saved_tool': None if self.session is None else self.session['tool']}


def load_config(config):
    return Creator5PauseDock(config)
