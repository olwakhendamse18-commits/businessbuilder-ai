import unittest
from pathlib import Path


class RealtimeVoiceClientContractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.source = (
            cls.root / "static/js/realtime-voice.js"
        ).read_text(encoding="utf-8")

    def test_only_completed_input_transcripts_reach_business_agent(self):
        self.assertIn(
            'const INPUT_TRANSCRIPT_COMPLETED = '
            '"conversation.item.input_audio_transcription.completed";',
            self.source,
        )
        self.assertIn("case INPUT_TRANSCRIPT_COMPLETED:", self.source)
        self.assertNotIn("type.includes", self.source)
        self.assertNotIn('type: "session.update"', self.source)

    def test_voice_handshake_keeps_runtime_preference_and_csrf_gates(self):
        self.assertIn("if (!config.voiceRuntimeEnabled)", self.source)
        self.assertIn("if (!config.voicePreferenceEnabled)", self.source)
        self.assertIn('"X-BusinessBuilder-CSRF"', self.source)
        self.assertIn('"X-BusinessBuilder-Voice-Request-ID"', self.source)

    def test_canonical_reply_uses_owned_out_of_band_audio_response(self):
        self.assertIn('conversation: "none"', self.source)
        self.assertIn('output_modalities: ["audio"]', self.source)
        self.assertIn("businessbuilder_request_id: requestId", self.source)
        self.assertIn(
            "this.responseRequestId(payload) !== this.activeSpeechRequestId",
            self.source,
        )

    def test_cancellation_does_not_clear_pending_approval(self):
        stop_speaking = self.source.split("stopSpeaking() {", 1)[1].split(
            "setMuted(muted) {", 1
        )[0]
        self.assertIn("if (this.pendingApproval)", stop_speaking)
        self.assertNotIn("this.pendingApproval = false", stop_speaking)

    def test_stopped_sessions_ignore_late_agent_turns(self):
        self.assertIn("this.sessionGeneration += 1", self.source)
        self.assertIn(
            "generation !== this.sessionGeneration || !this.peerConnection",
            self.source,
        )

    def test_failed_or_persistently_disconnected_peers_end_locally(self):
        self.assertIn('this.stop("peer_connection_failed", true)', self.source)
        self.assertIn('this.stop("peer_connection_disconnected", true)', self.source)
        self.assertIn("this.disconnectTimer", self.source)


if __name__ == "__main__":
    unittest.main()
