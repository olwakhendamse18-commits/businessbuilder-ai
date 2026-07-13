(function () {
    "use strict";

    const config = window.BB_COMMAND_CENTER || {};
    const voiceConsole = document.getElementById("voiceConsole");
    const textModeButton = document.getElementById("textModeButton");
    const voiceModeButton = document.getElementById("voiceModeButton");
    const startVoiceButton = document.getElementById("startVoiceButton");
    const stopVoiceButton = document.getElementById("stopVoiceButton");
    const muteVoiceButton = document.getElementById("muteVoiceButton");
    const interruptVoiceButton = document.getElementById("interruptVoiceButton");
    const fallbackTextButton = document.getElementById("fallbackTextButton");
    const voiceStateLabel = document.getElementById("voiceStateLabel");
    const voiceCaption = document.getElementById("voiceCaption");
    const voiceIndicator = document.getElementById("voiceIndicator");
    const voiceDuration = document.getElementById("voiceDuration");
    const transcript = document.getElementById("commandTranscript");
    const form = document.getElementById("commandChatForm");
    const canvas = document.getElementById("voiceWaveform");
    const remoteAudio = document.getElementById("voiceRemoteAudio");

    let voice = null;
    let voiceMode = false;
    let muted = false;

    function formatDuration(seconds) {
        const safe = Math.max(0, Number(seconds) || 0);
        const minutes = Math.floor(safe / 60);
        const remainder = safe % 60;
        return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
    }

    function addTranscriptMessage(role, content) {
        if (!transcript || !content) return;
        const wrapper = document.createElement("div");
        wrapper.className = `command-message ${role}`;
        const label = document.createElement("span");
        label.textContent = role === "user" ? "You" : "Builder";
        const paragraph = document.createElement("p");
        paragraph.textContent = content;
        wrapper.append(label, paragraph);
        transcript.appendChild(wrapper);
        transcript.scrollTop = transcript.scrollHeight;
    }

    function setVoiceState(state, detail) {
        if (voiceStateLabel) {
            voiceStateLabel.textContent = state.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
        }
        if (voiceCaption) {
            voiceCaption.textContent = detail || "Voice mode is ready.";
        }
        if (voiceIndicator) {
            voiceIndicator.dataset.state = state;
        }
        document.body.dataset.voiceState = state;
        const connected = ["connected", "listening", "user_speaking", "processing_transcript", "thinking", "waiting_for_approval", "speaking", "muted"].includes(state);
        if (startVoiceButton) startVoiceButton.disabled = connected;
        if (stopVoiceButton) stopVoiceButton.disabled = !connected;
        if (muteVoiceButton) muteVoiceButton.disabled = !connected;
        if (interruptVoiceButton) interruptVoiceButton.disabled = !connected;
        if (state === "waiting_for_approval") {
            addTranscriptMessage("assistant", "I’ve prepared that action, but it requires your approval. Please review the approval card before I continue.");
        }
    }

    function setMode(mode) {
        voiceMode = mode === "voice";
        if (voiceConsole) voiceConsole.hidden = !voiceMode;
        if (voiceModeButton) {
            voiceModeButton.classList.toggle("primary-button", voiceMode);
            voiceModeButton.classList.toggle("secondary-button", !voiceMode);
        }
        if (textModeButton) {
            textModeButton.classList.toggle("primary-button", !voiceMode);
            textModeButton.classList.toggle("secondary-button", voiceMode);
        }
        if (!voiceMode && voice) {
            voice.stop("switched_to_text");
        }
    }

    function ensureVoice() {
        if (!voice) {
            const waveform = window.BusinessBuilderWaveform ? new window.BusinessBuilderWaveform(canvas) : null;
            voice = new window.BusinessBuilderRealtimeVoice({
                waveform,
                remoteAudio,
                onState: setVoiceState,
                onTranscript: (message) => addTranscriptMessage("user", message),
                onCanonicalResponse: (payload) => {
                    if (form && payload.conversation_id) {
                        form.dataset.conversationId = payload.conversation_id;
                    }
                    addTranscriptMessage("assistant", payload.reply || "");
                },
                onDuration: (seconds, maxSeconds) => {
                    if (voiceDuration) {
                        voiceDuration.textContent = `${formatDuration(seconds)} / ${formatDuration(maxSeconds)}`;
                    }
                    if (maxSeconds - seconds === 60 && voiceCaption) {
                        voiceCaption.textContent = "Voice will disconnect in about one minute. Text mode remains available.";
                    }
                }
            });
        }
        return voice;
    }

    if (voiceModeButton) {
        voiceModeButton.addEventListener("click", () => setMode("voice"));
    }

    if (textModeButton) {
        textModeButton.addEventListener("click", () => setMode("text"));
    }

    if (fallbackTextButton) {
        fallbackTextButton.addEventListener("click", () => setMode("text"));
    }

    if (startVoiceButton) {
        startVoiceButton.addEventListener("click", async () => {
            setMode("voice");
            await ensureVoice().start();
        });
    }

    if (stopVoiceButton) {
        stopVoiceButton.addEventListener("click", async () => {
            if (voice) await voice.stop("user_stopped");
        });
    }

    if (muteVoiceButton) {
        muteVoiceButton.addEventListener("click", () => {
            muted = !muted;
            ensureVoice().setMuted(muted);
            muteVoiceButton.textContent = muted ? "Unmute" : "Mute";
            muteVoiceButton.setAttribute("aria-pressed", muted ? "true" : "false");
        });
    }

    if (interruptVoiceButton) {
        interruptVoiceButton.addEventListener("click", () => {
            ensureVoice().stopSpeaking();
        });
    }

    window.addEventListener("beforeunload", () => {
        if (voice) voice.stop("page_unload");
    });

    document.addEventListener("visibilitychange", () => {
        if (document.hidden && voiceCaption && voiceMode) {
            voiceCaption.textContent = "Tab is in the background. Voice remains connected only while your browser allows it.";
        }
    });

    setMode("text");
    setVoiceState("idle", "Voice is idle. Click Voice, then Start Voice when you are ready.");
})();
