import unittest
from pathlib import Path


class CommandVisualContractTestCase(unittest.TestCase):
    def test_visual_controller_supports_command_center_state_contract(self):
        root = Path(__file__).resolve().parents[1]
        controller = (
            root / "static/js/command-visual-state.js"
        ).read_text(encoding="utf-8")
        command_center = (
            root / "static/js/command-center.js"
        ).read_text(encoding="utf-8")

        self.assertIn("setState(nextState, options)", controller)
        self.assertIn("return this.update(nextState, options);", controller)
        self.assertIn("visualController.setState(", command_center)


if __name__ == "__main__":
    unittest.main()
