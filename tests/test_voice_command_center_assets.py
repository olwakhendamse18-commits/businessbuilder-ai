import unittest
from pathlib import Path


class VoiceCommandCenterAssetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.template = (
            cls.root / "templates/voice_command_center.html"
        ).read_text(encoding="utf-8")
        cls.script = (
            cls.root / "static/js/voice-command-center.js"
        ).read_text(encoding="utf-8")
        cls.stylesheet = (
            cls.root / "static/css/voice-command-center.css"
        ).read_text(encoding="utf-8")

    def test_focus_workspace_exposes_one_mission_and_four_live_phases(self):
        self.assertIn('id="voiceNextMissionTitle"', self.template)
        self.assertIn('id="voiceCinematicToggle"', self.template)
        self.assertEqual(self.template.count("data-voice-phase-step="), 4)
        self.assertIn('data-voice-phase="ready"', self.template)

    def test_phase_mapping_preserves_approval_as_a_distinct_state(self):
        self.assertIn("livePhaseByState", self.script)
        self.assertIn('waiting_for_approval: "approval"', self.script)
        self.assertIn('approval: "Approval Required"', self.script)

    def test_context_panels_use_progressive_disclosure(self):
        self.assertIn('aria-expanded="false"', self.template)
        self.assertIn(".voice-panel.is-open", self.stylesheet)
        self.assertNotIn(
            "if (!panel || !drawerMediaQuery.matches)", self.script
        )

    def test_cinematic_mode_is_optional(self):
        self.assertIn("setCinematicMode", self.script)
        self.assertIn("body.voice-cinematic", self.stylesheet)
        self.assertIn("Cinematic: Off", self.template)

    def test_versioned_voice_assets_are_loaded(self):
        self.assertIn("voice-command-center.css?v=20260803-voice", self.template)
        self.assertIn("realtime-voice.js?v=20260803-voice", self.template)
        self.assertIn("voice-command-center.js?v=20260803-voice", self.template)


if __name__ == "__main__":
    unittest.main()
