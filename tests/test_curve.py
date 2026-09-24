import unittest

from ugreen_fan.curve import interpolate, next_pwm

POINTS = ((40.0, 100), (50.0, 200))


class InterpolateTest(unittest.TestCase):
    def test_clamps_below_first_point(self):
        self.assertEqual(interpolate(POINTS, 30), 100)

    def test_clamps_above_last_point(self):
        self.assertEqual(interpolate(POINTS, 70), 200)

    def test_exact_point(self):
        self.assertEqual(interpolate(POINTS, 50), 200)

    def test_linear_between_points(self):
        self.assertEqual(interpolate(POINTS, 45), 150)

    def test_multiple_segments(self):
        points = ((40.0, 60), (46.0, 100), (48.0, 128))
        self.assertEqual(interpolate(points, 47), 114)


class NextPwmTest(unittest.TestCase):
    def test_rises_immediately(self):
        self.assertEqual(next_pwm(POINTS, 45, current=100, hysteresis=2), 150)

    def test_holds_within_hysteresis(self):
        self.assertEqual(next_pwm(POINTS, 44, current=150, hysteresis=2), 150)

    def test_falls_once_temperature_dropped_by_hysteresis(self):
        self.assertEqual(next_pwm(POINTS, 42, current=150, hysteresis=2), 140)

    def test_start_from_failsafe_descends_to_shifted_curve(self):
        self.assertEqual(next_pwm(POINTS, 45, current=255, hysteresis=2), 170)

    def test_zero_hysteresis_follows_curve(self):
        self.assertEqual(next_pwm(POINTS, 42, current=150, hysteresis=0), 120)
