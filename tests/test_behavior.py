"""Offline behavioral tests. Motion, thermal response and sensors are simulated."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location('candidate', ROOT.parent/'klippy/oc_pause_dock.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Command:
    error = RuntimeError

    def __init__(self, **params):
        self.params = params

    def get_float(self, key, default=None, above=None):
        value = float(self.params.get(key, default))
        if above is not None and value <= above:
            raise RuntimeError('Invalid parameter')
        return value

    def respond_info(self, text):
        pass


class Status:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return self.values


class Rig:
    def __init__(self, tool=1):
        self.events = []
        self.fail = None
        self.wait_callback = lambda: None
        self.attached = tool
        self.saved_position = None
        self.saved_tool = tool
        self.pause = SimpleNamespace(is_paused=False)
        self.lane = SimpleNamespace(name='extruder%d' % tool,
                                    extruder_obj=SimpleNamespace(creator5_tool_index=tool,
                                                                lane_loaded='extruder%d' % tool))
        self.lane.set_tool_loaded = self.loaded
        self.afc = SimpleNamespace(current=self.lane.name, in_toolchange=False, error_state=False)
        self.tc = SimpleNamespace(busy=False, safe_z=10., approach_x=250.,
                                  clear_travel_speed=600., pickup_accel=30000.,
                                  pickup_minimum_cruise_ratio=0.,
                                  attached_tool_from_pins=lambda: self.attached,
                                  _afc_lane=lambda afc, name: self.lane)
        self.toolhead = Status(position=[180., 100., 3., 45.], homed_axes='xyz',
                               axis_minimum=[-2., -5., -10.], axis_maximum=[310., 260., 270.],
                               max_velocity=600., max_accel=500., minimum_cruise_ratio=.5)
        self.toolhead.manual_move = self.move
        self.objects = {'gcode': self, 'pause_resume': self.pause, 'toolhead': self.toolhead,
                        'creator5_toolchanger': self.tc, 'AFC': self.afc,
                        'gcode_move': SimpleNamespace(reset_last_position=lambda: None),
                        'print_stats': Status(filename='test.gcode'),
                        'idle_timeout': Status(idle_timeout=600.)}
        for n in range(4):
            self.objects['extruder' if n == 0 else 'extruder%d' % n] = Status(target=285.)
        config = SimpleNamespace(get_printer=lambda: self,
                                 getfloat=lambda name, default, **kwargs: default)
        self.subject = module.Creator5PauseDock(config)

    def register_event_handler(self, name, handler):
        pass

    def register_command(self, name, handler):
        pass

    def lookup_object(self, name):
        return self.objects[name]

    def get_reactor(self):
        return SimpleNamespace(monotonic=lambda: 1.)

    def loaded(self, normal_toolchange):
        self.events.append('AFC_LOADED')
        self.lane.extruder_obj.lane_loaded = self.lane.name
        self.afc.current = self.lane.name

    def move(self, coordinates, speed):
        self.events.append(('MOVE', list(coordinates), speed))
        for i, value in enumerate(coordinates):
            if value is not None:
                self.toolhead.values['position'][i] = value

    def run_script_from_command(self, script):
        self.events.append(script)
        if self.fail and script.startswith(self.fail):
            raise RuntimeError('Injected failure')
        if script == 'PAUSE_BASE':
            self.pause.is_paused = True
            self.saved_position = self.toolhead.values['position'][:]
        elif script == 'C5_TOOL_DOCK':
            self.attached = None
            self.afc.current = None
            self.lane.extruder_obj.lane_loaded = None
            self.toolhead.values['position'][:2] = [250., 106.]
        elif script.startswith('C5_TOOL_SELECT'):
            self.attached = self.saved_tool
            self.toolhead.values['position'][:2] = [236., 106.]
        elif script.startswith('TEMPERATURE_WAIT'):
            self.wait_callback()
        elif script.startswith('RESUME_BASE'):
            self.pause.is_paused = False
            self.toolhead.values['position'] = self.saved_position[:]

    def pause_print(self, **params):
        self.subject.cmd_pause(Command(**params))

    def resume(self, **params):
        self.subject.cmd_resume(Command(**params))


class BehaviorTests(unittest.TestCase):
    def test_all_four_tools_and_full_order(self):
        for tool in range(4):
            with self.subTest(tool=tool):
                rig = Rig(tool)
                rig.pause_print()
                s = rig.subject.session
                self.assertEqual(s['target'], 285.)
                self.assertEqual(rig.toolhead.values['position'][:3], [125., 220., 10.])
                self.assertIsNone(rig.attached)
                rig.resume()
                events = rig.events
                heater = 'extruder' if tool == 0 else 'extruder%d' % tool
                self.assertLess(events.index('PAUSE_BASE'), events.index('C5_TOOL_DOCK'))
                self.assertLess(events.index('C5_TOOL_DOCK'), events.index('SET_HEATER_TEMPERATURE HEATER=%s TARGET=0' % heater))
                wait = next(i for i, e in enumerate(events) if isinstance(e, str) and e.startswith('TEMPERATURE_WAIT'))
                self.assertLess(wait, events.index('C5_TOOL_SELECT T=%d' % tool))
                self.assertLess(events.index('AFC_LOADED'), events.index(('MOVE', [180., 100., None], 600.)))
                self.assertEqual(events[-1], 'RESUME_BASE VELOCITY=10.000')
                self.assertEqual(rig.toolhead.values['position'], [180., 100., 3., 45.])
                self.assertEqual(rig.subject.phase, 'idle')

    def test_repeated_pause_keeps_original_state(self):
        rig = Rig()
        rig.pause_print()
        original = rig.subject.session
        rig.pause_print()
        self.assertIs(rig.subject.session, original)
        self.assertEqual(rig.events.count('PAUSE_BASE'), 1)

    def test_temperature_wait_really_precedes_pickup(self):
        rig = Rig()
        rig.pause_print()
        def check_wait():
            self.assertIsNone(rig.attached)
            self.assertEqual(rig.toolhead.values['position'][:2], [125., 220.])
            self.assertTrue(rig.pause.is_paused)
        rig.wait_callback = check_wait
        rig.resume()

    def test_docking_failure_never_cools_or_parks(self):
        rig = Rig()
        rig.fail = 'C5_TOOL_DOCK'
        with self.assertRaises(RuntimeError):
            rig.pause_print()
        self.assertTrue(rig.pause.is_paused)
        self.assertEqual(rig.attached, 1)
        self.assertFalse(any(isinstance(e, str) and 'TARGET=0' in e for e in rig.events))
        self.assertNotIn(('MOVE', [125., 220., None], 600.), rig.events)

    def test_heat_or_pickup_failure_never_resumes(self):
        for fault in ('TEMPERATURE_WAIT', 'C5_TOOL_SELECT'):
            with self.subTest(fault=fault):
                rig = Rig()
                rig.pause_print()
                rig.fail = fault
                with self.assertRaises(RuntimeError):
                    rig.resume()
                self.assertTrue(rig.pause.is_paused)
                self.assertFalse(any(isinstance(e, str) and e.startswith('RESUME_BASE') for e in rig.events))

    def test_insufficient_z_refuses_xy_motion(self):
        rig = Rig()
        rig.toolhead.values['position'][2] = 268.
        with self.assertRaises(RuntimeError):
            rig.pause_print()
        self.assertTrue(rig.pause.is_paused)
        self.assertFalse(any(isinstance(e, tuple) for e in rig.events))

    def test_resume_without_session_refused(self):
        rig = Rig()
        rig.pause.is_paused = True
        with self.assertRaises(RuntimeError):
            rig.resume()
        self.assertEqual(rig.events, [])

    def test_lost_homing_refused_without_heat_or_motion(self):
        rig = Rig()
        rig.pause_print()
        rig.toolhead.values['homed_axes'] = 'xy'
        rig.events.clear()
        with self.assertRaises(RuntimeError):
            rig.resume()
        self.assertFalse(any(isinstance(e, tuple) or 'TEMPERATURE' in e for e in rig.events))

    def test_afc_error_refused(self):
        rig = Rig()
        rig.pause_print()
        rig.afc.error_state = True
        with self.assertRaises(RuntimeError):
            rig.resume()
        self.assertTrue(rig.pause.is_paused)

    def test_manual_tool_pickup_refused(self):
        rig = Rig()
        rig.pause_print()
        rig.attached = 2
        with self.assertRaises(RuntimeError):
            rig.resume()

    def test_changed_job_refused(self):
        rig = Rig()
        rig.pause_print()
        rig.objects['print_stats'].values['filename'] = 'other.gcode'
        with self.assertRaises(RuntimeError):
            rig.resume()

    def test_velocity_capped_and_invalid_values_refused(self):
        rig = Rig()
        rig.pause_print()
        rig.resume(VELOCITY=900)
        self.assertIn(('MOVE', [180., 100., None], 600.), rig.events)
        for value in (-1, 0, float('nan'), float('inf')):
            with self.subTest(value=value):
                rig = Rig()
                rig.pause_print()
                with self.assertRaises(RuntimeError):
                    rig.resume(VELOCITY=value)

    def test_cancel_during_heating_prevents_pickup(self):
        rig = Rig()
        rig.pause_print()
        def cancel():
            rig.subject.cmd_reset(Command())
            rig.pause.is_paused = False
        rig.wait_callback = cancel
        with self.assertRaises(RuntimeError):
            rig.resume()
        self.assertFalse(any(isinstance(e, str) and e.startswith('C5_TOOL_SELECT') for e in rig.events))

    def test_manual_z_jog_is_lifted_again_before_pickup(self):
        rig = Rig()
        rig.pause_print()
        rig.toolhead.values['position'][2] = 4.
        rig.events.clear()
        rig.resume()
        self.assertIn(('MOVE', [None, None, 10.], 10.), rig.events)

    def test_print_acceleration_and_timeout_restored(self):
        rig = Rig()
        rig.pause_print()
        rig.resume()
        self.assertIn('SET_IDLE_TIMEOUT TIMEOUT=36000.000', rig.events)
        self.assertIn('SET_IDLE_TIMEOUT TIMEOUT=600.000', rig.events)
        self.assertIn('SET_VELOCITY_LIMIT ACCEL=30000.000', rig.events)
        self.assertIn('SET_VELOCITY_LIMIT ACCEL=500.000', rig.events)
        self.assertIn('SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.500000', rig.events)


if __name__ == '__main__':
    unittest.main(verbosity=2)
