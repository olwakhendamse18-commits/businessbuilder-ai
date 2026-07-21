(function () {
    "use strict";

    const config = window.BB_COMMAND_CENTER || {};

    function textContent(value) {
        return String(value || "").trim();
    }

    function createClientRequestId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return `bb-voice-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }

    class BusinessBuilderRealtimeVoice {
        constructor(options) {
            this.options = options || {};
            this.peerConnection = null;
            this.dataChannel = null;
            this.localStream = null;
            this.remoteStream = null;
            this.voiceSessionId = null;
            this.conversationId = config.conversationId;
            this.lastTranscript = "";
            this.lastTranscriptAt = 0;
            this.startedAt = null;
            this.durationTimer = null;
            this.maxTimer = null;
            this.idleTimer = null;
            this.muted = false;
            this.waveform = this.options.waveform;
            this.state = "idle";
        }

        setState(state, detail) {
            this.state = state;
            if (typeof this.options.onState === "function") {
                this.options.onState(state, detail || "");
            }
            if (this.waveform) {
                if (state === "listening" || state === "user_speaking") this.waveform.setMode("listening");
                else if (state === "speaking") this.waveform.setMode("speaking");
                else if (state === "waiting_for_approval") this.waveform.setMode("approval");
                else if (state === "error") this.waveform.setMode("error");
            }
        }

        async start() {
            if (this.peerConnection) return;
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                this.setState("error", "This browser does not support microphone voice mode.");
                return;
            }
            if (!window.RTCPeerConnection) {
                this.setState("error", "This browser does not support WebRTC voice mode.");
                return;
            }

            this.setState("requesting_microphone", "Requesting microphone permission.");
            try {
                this.localStream = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
            } catch (error) {
                this.setState("error", "Microphone permission was denied or no microphone is available.");
                await this.stop("microphone_unavailable");
                return;
            }

            this.setState("connecting", "Connecting secure voice session.");
            this.peerConnection = new RTCPeerConnection();
            this.remoteStream = new MediaStream();
            this.peerConnection.ontrack = (event) => {
                event.streams[0].getTracks().forEach((track) => this.remoteStream.addTrack(track));
                if (this.options.remoteAudio) this.options.remoteAudio.srcObject = this.remoteStream;
                this.setState("speaking", "Builder is speaking.");
            };

            this.peerConnection.onconnectionstatechange = () => {
                const state = this.peerConnection ? this.peerConnection.connectionState : "closed";
                if (state === "connected") this.setState("connected", "Voice connected.");
                if (state === "failed") this.setState("error", "Voice connection failed. Text mode still works.");
                if (state === "disconnected") this.setState("disconnected", "Voice disconnected.");
            };

            this.dataChannel = this.peerConnection.createDataChannel("oai-events");
            this.dataChannel.onmessage = (event) => this.handleRealtimeEvent(event);
            this.dataChannel.onopen = () => {
                this.setState("listening", "Listening.");
                this.configureRealtimeSession();
            };

            this.localStream.getTracks().forEach((track) => this.peerConnection.addTrack(track, this.localStream));
            if (this.waveform) this.waveform.attachStream(this.localStream);

            try {
                const offer = await this.peerConnection.createOffer();
                await this.peerConnection.setLocalDescription(offer);
                const response = await fetch(config.realtimeSessionUrl || "/api/realtime/session", {
                    method: "POST",
                    headers: {"Content-Type": "application/sdp"},
                    body: offer.sdp
                });
                if (!response.ok) {
                    const type = response.headers.get("content-type") || "";
                    let message = "Voice could not start. Text mode is still available.";
                    if (type.includes("application/json")) {
                        const payload = await response.json();
                        message = payload.error || message;
                    }
                    throw new Error(message);
                }
                this.voiceSessionId = response.headers.get("X-BusinessBuilder-Voice-Session");
                this.conversationId = response.headers.get("X-BusinessBuilder-Conversation") || this.conversationId;
                const maxSeconds = Number(response.headers.get("X-BusinessBuilder-Voice-Max-Seconds") || config.maxVoiceSeconds || 600);
                const answerSdp = await response.text();
                await this.peerConnection.setRemoteDescription({type: "answer", sdp: answerSdp});
                this.startTimers(maxSeconds);
                this.setState("connected", "Voice connected.");
            } catch (error) {
                this.setState("error", error.message);
                await this.stop("handshake_failed");
            }
        }

        configureRealtimeSession() {
            this.sendRealtimeEvent({
                type: "session.update",
                session: {
                    modalities: ["audio", "text"],
                    instructions: "Use voice only as BusinessBuilder AI's transport. Wait for backend canonical responses before speaking business advice.",
                    turn_detection: {
                        type: "semantic_vad",
                        eagerness: "low",
                        create_response: false,
                        interrupt_response: true
                    },
                    input_audio_transcription: {model: "gpt-4o-mini-transcribe"},
                    tool_choice: "none"
                }
            });
        }

        sendRealtimeEvent(payload) {
            if (this.dataChannel && this.dataChannel.readyState === "open") {
                this.dataChannel.send(JSON.stringify(payload));
            }
        }

        handleRealtimeEvent(event) {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (error) {
                return;
            }
            const type = payload.type || "";
            if (type.includes("speech_started")) {
                this.setState("user_speaking", "You are speaking.");
                this.stopSpeaking();
            }
            if (type.includes("speech_stopped")) {
                this.setState("processing_transcript", "Processing transcript.");
            }
            const transcript =
                payload.transcript ||
                (payload.item && payload.item.content && payload.item.content[0] && payload.item.content[0].transcript) ||
                (payload.response && payload.response.output_text) ||
                "";
            if (transcript && (type.includes("transcription.completed") || type.includes("transcript.done") || type.includes("input_audio"))) {
                this.submitCompletedTranscript(transcript);
            }
        }

        async submitCompletedTranscript(transcript) {
            const message = textContent(transcript);
            if (!message) {
                this.setState("listening", "Empty transcript ignored.");
                return;
            }
            const now = Date.now();
            if (message === this.lastTranscript && now - this.lastTranscriptAt < 4000) {
                return;
            }
            this.lastTranscript = message;
            this.lastTranscriptAt = now;
            this.resetIdleTimer();
            this.setState("thinking", "Builder is thinking.");
            if (typeof this.options.onTranscript === "function") {
                this.options.onTranscript(message);
            }
            try {
                const response = await fetch(config.agentMessageUrl || "/api/agent/message", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"Accept": "application/json", "Content-Type": "application/json"},
                    body: JSON.stringify({
                        message,
                        conversation_id: this.conversationId,
                        mode: "voice",
                        request_id: createClientRequestId()
                    })
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Builder could not respond.");
                this.conversationId = payload.conversation_id || this.conversationId;
                if (typeof this.options.onCanonicalResponse === "function") {
                    this.options.onCanonicalResponse(payload);
                }
                this.speakCanonicalResponse(payload.reply || "");
                this.setState(payload.approval_needed ? "waiting_for_approval" : "speaking", payload.approval_needed ? "Approval required." : "Builder is speaking.");
            } catch (error) {
                this.setState("error", error.message);
            }
        }

        speakCanonicalResponse(text) {
            const spoken = textContent(text);
            if (!spoken) return;
            this.sendRealtimeEvent({type: "response.cancel"});
            this.sendRealtimeEvent({
                type: "response.create",
                response: {
                    modalities: ["audio", "text"],
                    instructions: "Speak this canonical BusinessBuilder AI response faithfully. Do not add facts or change approval wording:\n\n" + spoken
                }
            });
        }

        stopSpeaking() {
            this.sendRealtimeEvent({type: "response.cancel"});
            if (this.options.remoteAudio) {
                try {
                    this.options.remoteAudio.pause();
                    this.options.remoteAudio.currentTime = 0;
                    this.options.remoteAudio.play().catch(() => {});
                } catch (error) {}
            }
            this.setState("listening", "Listening.");
        }

        setMuted(muted) {
            this.muted = Boolean(muted);
            if (this.localStream) {
                this.localStream.getAudioTracks().forEach((track) => { track.enabled = !this.muted; });
            }
            this.setState(this.muted ? "muted" : "listening", this.muted ? "Microphone muted." : "Microphone unmuted.");
        }

        startTimers(maxSeconds) {
            this.startedAt = Date.now();
            this.durationTimer = window.setInterval(() => {
                if (typeof this.options.onDuration === "function") {
                    this.options.onDuration(Math.floor((Date.now() - this.startedAt) / 1000), maxSeconds);
                }
            }, 1000);
            this.maxTimer = window.setTimeout(() => {
                this.setState("stopped", "Voice session reached the maximum duration.");
                this.stop("max_duration_reached");
            }, Math.max(1, maxSeconds) * 1000);
            this.resetIdleTimer();
        }

        resetIdleTimer() {
            if (this.idleTimer) window.clearTimeout(this.idleTimer);
            const idleSeconds = Number(config.idleTimeoutSeconds || 90);
            this.idleTimer = window.setTimeout(() => {
                this.setState("stopped", "Voice session stopped after inactivity.");
                this.stop("idle_timeout");
            }, idleSeconds * 1000);
        }

        async stop(reason) {
            if (this.durationTimer) window.clearInterval(this.durationTimer);
            if (this.maxTimer) window.clearTimeout(this.maxTimer);
            if (this.idleTimer) window.clearTimeout(this.idleTimer);
            this.durationTimer = null;
            this.maxTimer = null;
            this.idleTimer = null;

            if (this.dataChannel) {
                try { this.dataChannel.close(); } catch (error) {}
            }
            if (this.peerConnection) {
                try { this.peerConnection.close(); } catch (error) {}
            }
            if (this.localStream) {
                this.localStream.getTracks().forEach((track) => track.stop());
            }
            if (this.remoteStream) {
                this.remoteStream.getTracks().forEach((track) => track.stop());
            }
            if (this.options.remoteAudio) {
                try {
                    this.options.remoteAudio.pause();
                    this.options.remoteAudio.srcObject = null;
                } catch (error) {}
            }
            if (this.waveform) this.waveform.stop();

            const sessionId = this.voiceSessionId;
            this.peerConnection = null;
            this.dataChannel = null;
            this.localStream = null;
            this.remoteStream = null;
            this.voiceSessionId = null;
            this.setState("stopped", "Voice stopped. Text mode is available.");

            if (sessionId) {
                try {
                    await fetch(config.realtimeEndUrl || "/api/realtime/session/end", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({voice_session_id: sessionId, reason: reason || "client_disconnected"})
                    });
                } catch (error) {}
            }
        }
    }

    window.BusinessBuilderRealtimeVoice = BusinessBuilderRealtimeVoice;
})();
