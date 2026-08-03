(function () {
    "use strict";

    const config = window.BB_VOICE_COMMAND_CENTER || window.BB_COMMAND_CENTER || {};
    const INPUT_TRANSCRIPT_COMPLETED = "conversation.item.input_audio_transcription.completed";
    const APPROVAL_SPEECH = "I prepared that request, but it needs your approval. Please review the approval card before I continue.";

    function cleanText(value) {
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
            this.handshakeRequestId = null;
            this.conversationId = config.conversationId;
            this.startedAt = null;
            this.durationTimer = null;
            this.maxTimer = null;
            this.idleTimer = null;
            this.disconnectTimer = null;
            this.muted = false;
            this.pendingApproval = false;
            this.activeSpeechRequestId = null;
            this.sessionGeneration = 0;
            this.waveform = this.options.waveform;
            this.state = "idle";
            this.processedItemIds = new Set();
            this.turnQueue = Promise.resolve();
            this.stopPromise = null;
        }

        setState(state, detail) {
            this.state = state;
            if (typeof this.options.onState === "function") {
                this.options.onState(state, detail || "");
            }
            if (!this.waveform) return;
            if (["listening", "user_speaking"].includes(state)) this.waveform.setMode("listening");
            else if (state === "speaking") this.waveform.setMode("speaking");
            else if (state === "waiting_for_approval") this.waveform.setMode("approval");
            else if (state === "error") this.waveform.setMode("error");
            else this.waveform.setMode("idle");
        }

        async start() {
            if (this.peerConnection || this.stopPromise) return false;
            if (!config.voiceRuntimeEnabled) {
                this.setState("unavailable", "Live voice is not enabled on this server. Text mode remains available.");
                return false;
            }
            if (!config.voicePreferenceEnabled) {
                this.setState("unavailable", "Voice is off in your Builder preferences. Enable it before starting a microphone session.");
                return false;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                this.setState("error", "This browser does not support microphone voice mode.");
                return false;
            }
            if (!window.RTCPeerConnection) {
                this.setState("error", "This browser does not support secure WebRTC voice mode.");
                return false;
            }

            this.sessionGeneration += 1;
            this.processedItemIds.clear();
            this.turnQueue = Promise.resolve();

            this.setState("requesting_microphone", "Choose Allow to start a voice-only Builder session.");
            try {
                this.localStream = await navigator.mediaDevices.getUserMedia({audio: true, video: false});
            } catch (error) {
                await this.stop("microphone_unavailable", true);
                this.setState("error", "Microphone permission was denied or no microphone is available.");
                return false;
            }

            this.setState("connecting", "Connecting a protected voice session.");
            this.peerConnection = new RTCPeerConnection();
            this.remoteStream = new MediaStream();
            this.peerConnection.ontrack = (event) => {
                const streams = event.streams || [];
                streams.forEach((stream) => {
                    stream.getTracks().forEach((track) => this.remoteStream.addTrack(track));
                });
                if (this.options.remoteAudio) {
                    this.options.remoteAudio.srcObject = this.remoteStream;
                }
            };
            this.peerConnection.onconnectionstatechange = () => {
                const connectionState = this.peerConnection ? this.peerConnection.connectionState : "closed";
                if (connectionState === "connected") {
                    if (this.disconnectTimer) window.clearTimeout(this.disconnectTimer);
                    this.disconnectTimer = null;
                    if (["connecting", "disconnected"].includes(this.state)) {
                        this.setState("listening", "Voice connected. I’m listening for your business question.");
                    }
                } else if (connectionState === "failed") {
                    this.stop("peer_connection_failed", true).finally(() => {
                        this.setState("error", "Voice connection failed safely. Text mode still works.");
                    });
                } else if (connectionState === "disconnected") {
                    this.setState("disconnected", "Voice disconnected. Your saved business work is unchanged.");
                    if (!this.disconnectTimer) {
                        this.disconnectTimer = window.setTimeout(() => {
                            this.disconnectTimer = null;
                            if (this.peerConnection && this.peerConnection.connectionState === "disconnected") {
                                this.stop("peer_connection_disconnected", true).finally(() => {
                                    this.setState("disconnected", "Voice disconnected. Your saved business work is unchanged.");
                                });
                            }
                        }, 5000);
                    }
                }
            };

            this.dataChannel = this.peerConnection.createDataChannel("oai-events");
            this.dataChannel.onmessage = (event) => this.handleRealtimeEvent(event);
            this.dataChannel.onopen = () => {
                this.setState("listening", "Voice connected. I’m listening for your business question.");
                this.resetIdleTimer();
            };

            this.localStream.getTracks().forEach((track) => this.peerConnection.addTrack(track, this.localStream));
            if (this.waveform) this.waveform.attachStream(this.localStream);

            try {
                const offer = await this.peerConnection.createOffer();
                await this.peerConnection.setLocalDescription(offer);
                this.handshakeRequestId = createClientRequestId();
                const response = await fetch(config.realtimeSessionUrl || "/api/realtime/session", {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/sdp",
                        "X-BusinessBuilder-CSRF": cleanText(config.voiceCsrfToken),
                        "X-BusinessBuilder-Voice-Request-ID": this.handshakeRequestId
                    },
                    body: offer.sdp
                });
                if (!response.ok) {
                    const contentType = response.headers.get("content-type") || "";
                    let message = "Voice could not start. Text mode is still available.";
                    if (contentType.includes("application/json")) {
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
                this.setState("listening", "Voice connected. I’m listening for your business question.");
                return true;
            } catch (error) {
                const message = error instanceof Error ? error.message : "Voice could not start safely.";
                await this.stop("handshake_failed", true);
                this.setState("error", message);
                return false;
            }
        }

        sendRealtimeEvent(payload) {
            if (this.dataChannel && this.dataChannel.readyState === "open") {
                this.dataChannel.send(JSON.stringify(payload));
                return true;
            }
            return false;
        }

        handleRealtimeEvent(event) {
            let payload;
            try {
                payload = JSON.parse(event.data);
            } catch (error) {
                return;
            }

            switch (payload.type) {
            case "input_audio_buffer.speech_started":
                this.resetIdleTimer();
                this.cancelPlayback();
                this.setState("user_speaking", "I can hear you. Finish your thought when you’re ready.");
                break;
            case "input_audio_buffer.speech_stopped":
                this.setState("processing_transcript", "Turning your speech into one secure text request.");
                break;
            case INPUT_TRANSCRIPT_COMPLETED:
                this.queueCompletedTranscript(payload);
                break;
            case "response.created":
                if (
                    this.responseRequestId(payload) === this.activeSpeechRequestId
                    && !this.pendingApproval
                ) {
                    this.setState("speaking", "Builder is speaking.");
                }
                break;
            case "response.done":
                if (this.responseRequestId(payload) !== this.activeSpeechRequestId) break;
                this.activeSpeechRequestId = null;
                this.resetIdleTimer();
                if (payload.response && payload.response.status && payload.response.status !== "completed") {
                    this.setState("error", "Voice playback ended unexpectedly. Your business work is unchanged.");
                } else if (this.pendingApproval) {
                    this.setState("waiting_for_approval", "The proposed action is paused until you review it.");
                } else if (this.muted) {
                    this.setState("muted", "Microphone muted.");
                } else {
                    this.setState("listening", "I’m listening for your next business question.");
                }
                break;
            case "error":
                this.setState("error", "The voice service returned an error. No external action was taken.");
                break;
            default:
                break;
            }
        }

        responseRequestId(payload) {
            return cleanText(
                payload
                && payload.response
                && payload.response.metadata
                && payload.response.metadata.businessbuilder_request_id
            );
        }

        queueCompletedTranscript(payload) {
            const transcript = cleanText(payload.transcript);
            const itemId = cleanText(payload.item_id || (payload.item && payload.item.id));
            const generation = this.sessionGeneration;
            if (!transcript) {
                this.setState(this.muted ? "muted" : "listening", "Empty transcript ignored.");
                return;
            }
            if (itemId && this.processedItemIds.has(itemId)) return;
            if (itemId) {
                this.processedItemIds.add(itemId);
                if (this.processedItemIds.size > 100) {
                    this.processedItemIds.delete(this.processedItemIds.values().next().value);
                }
            }
            this.turnQueue = this.turnQueue
                .then(() => this.submitCompletedTranscript(transcript, itemId, generation))
                .catch((error) => {
                    if (generation !== this.sessionGeneration || (error && error.name === "AbortError")) return;
                    this.setState("error", "Builder could not process that voice turn safely.");
                });
        }

        async submitCompletedTranscript(transcript, itemId, generation) {
            const message = cleanText(transcript);
            if (!message || generation !== this.sessionGeneration || !this.peerConnection) return;
            this.resetIdleTimer();
            this.setState("thinking", "Builder is checking your project and choosing the safest next step.");
            if (typeof this.options.onTranscript === "function") {
                this.options.onTranscript(message, itemId);
            }
            const requestId = createClientRequestId();
            const response = await fetch(config.agentMessageUrl || "/api/agent/message", {
                method: "POST",
                credentials: "same-origin",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: JSON.stringify({
                    message,
                    conversation_id: this.conversationId,
                    mode: "voice",
                    request_id: requestId
                })
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Builder could not respond.");
            if (generation !== this.sessionGeneration || !this.peerConnection) return;
            this.conversationId = payload.conversation_id || this.conversationId;
            if (typeof this.options.onCanonicalResponse === "function") {
                this.options.onCanonicalResponse(payload);
            }
            this.pendingApproval = Boolean(payload.approval_needed);
            const speechStatus = this.speakCanonicalResponse(payload, requestId);
            if (this.pendingApproval) {
                this.setState("waiting_for_approval", "The proposed action is paused until you review it.");
            } else if (speechStatus === "queued") {
                this.setState("speaking", "Builder is speaking.");
            } else if (speechStatus === "empty") {
                this.setState(this.muted ? "muted" : "listening", "No spoken response was needed.");
            } else {
                this.setState("error", "Builder prepared a response, but voice playback could not start.");
            }
        }

        speakCanonicalResponse(payload, requestId) {
            const spoken = payload.approval_needed ? APPROVAL_SPEECH : cleanText(payload.reply);
            if (!spoken) return "empty";
            this.cancelPlayback();
            this.resetIdleTimer();
            if (this.options.remoteAudio) {
                this.options.remoteAudio.muted = false;
                this.options.remoteAudio.play().catch(() => {});
            }
            this.activeSpeechRequestId = requestId;
            const sent = this.sendRealtimeEvent({
                type: "response.create",
                response: {
                    conversation: "none",
                    output_modalities: ["audio"],
                    metadata: {businessbuilder_request_id: requestId},
                    instructions: "Read the single input message aloud exactly as written. Do not add, remove, summarize, explain, or change any wording.",
                    input: [{
                        type: "message",
                        role: "user",
                        content: [{type: "input_text", text: spoken}]
                    }]
                }
            });
            if (!sent) {
                this.activeSpeechRequestId = null;
                if (this.options.remoteAudio) this.options.remoteAudio.muted = true;
                return "unavailable";
            }
            return "queued";
        }

        cancelPlayback() {
            if (this.activeSpeechRequestId) {
                this.sendRealtimeEvent({type: "response.cancel"});
                this.activeSpeechRequestId = null;
            }
            if (this.options.remoteAudio) this.options.remoteAudio.muted = true;
        }

        stopSpeaking() {
            this.cancelPlayback();
            if (this.pendingApproval) {
                this.setState("waiting_for_approval", "The proposed action is still paused until you review it.");
            } else {
                this.setState(this.muted ? "muted" : "listening", "Builder stopped speaking. I’m listening again.");
            }
        }

        setMuted(muted) {
            this.muted = Boolean(muted);
            if (this.localStream) {
                this.localStream.getAudioTracks().forEach((track) => { track.enabled = !this.muted; });
            }
            this.setState(this.muted ? "muted" : "listening", this.muted ? "Microphone muted." : "Microphone unmuted. I’m listening.");
        }

        startTimers(maxSeconds) {
            this.startedAt = Date.now();
            this.durationTimer = window.setInterval(() => {
                if (typeof this.options.onDuration === "function") {
                    this.options.onDuration(Math.floor((Date.now() - this.startedAt) / 1000), maxSeconds);
                }
            }, 1000);
            this.maxTimer = window.setTimeout(() => {
                this.stop("max_duration_reached");
            }, Math.max(1, maxSeconds) * 1000);
            this.resetIdleTimer();
        }

        resetIdleTimer() {
            if (this.idleTimer) window.clearTimeout(this.idleTimer);
            if (this.disconnectTimer) window.clearTimeout(this.disconnectTimer);
            if (!this.peerConnection) return;
            const idleSeconds = Number(config.idleTimeoutSeconds || 90);
            this.idleTimer = window.setTimeout(() => {
                this.stop("idle_timeout");
            }, Math.max(20, idleSeconds) * 1000);
        }

        async stop(reason, silent) {
            if (this.stopPromise) return this.stopPromise;
            this.stopPromise = this.performStop(reason, silent);
            try {
                await this.stopPromise;
            } finally {
                this.stopPromise = null;
            }
        }

        async performStop(reason, silent) {
            this.sessionGeneration += 1;
            if (this.durationTimer) window.clearInterval(this.durationTimer);
            if (this.maxTimer) window.clearTimeout(this.maxTimer);
            if (this.idleTimer) window.clearTimeout(this.idleTimer);
            this.durationTimer = null;
            this.maxTimer = null;
            this.idleTimer = null;
            this.disconnectTimer = null;
            this.cancelPlayback();

            if (this.dataChannel) {
                try { this.dataChannel.close(); } catch (error) {}
            }
            if (this.peerConnection) {
                try { this.peerConnection.close(); } catch (error) {}
            }
            if (this.localStream) this.localStream.getTracks().forEach((track) => track.stop());
            if (this.remoteStream) this.remoteStream.getTracks().forEach((track) => track.stop());
            if (this.options.remoteAudio) {
                try {
                    this.options.remoteAudio.pause();
                    this.options.remoteAudio.srcObject = null;
                    this.options.remoteAudio.muted = false;
                } catch (error) {}
            }
            if (this.waveform) this.waveform.stop();

            const sessionId = this.voiceSessionId;
            const endRequestId = this.handshakeRequestId || createClientRequestId();
            this.peerConnection = null;
            this.dataChannel = null;
            this.localStream = null;
            this.remoteStream = null;
            this.voiceSessionId = null;
            this.handshakeRequestId = null;
            this.startedAt = null;
            this.muted = false;
            this.pendingApproval = false;
            this.activeSpeechRequestId = null;

            if (sessionId) {
                try {
                    await fetch(config.realtimeEndUrl || "/api/realtime/session/end", {
                        method: "POST",
                        credentials: "same-origin",
                        headers: {
                            "Content-Type": "application/json",
                            "X-BusinessBuilder-CSRF": cleanText(config.voiceCsrfToken),
                            "X-BusinessBuilder-Voice-Request-ID": endRequestId
                        },
                        body: JSON.stringify({voice_session_id: sessionId, reason: reason || "client_disconnected"})
                    });
                } catch (error) {}
            }
            if (!silent) this.setState("stopped", "Voice stopped. Your text workspace is still available.");
        }
    }

    window.BusinessBuilderRealtimeVoice = BusinessBuilderRealtimeVoice;
}());
