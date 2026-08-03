(function () {
    "use strict";

    const config = window.BB_VOICE_COMMAND_CENTER || {};
    const body = document.body;
    const shell = document.getElementById("voiceShell");
    const canvas = document.getElementById("voiceCoreCanvas");
    const context = canvas ? canvas.getContext("2d") : null;
    const stateTitle = document.getElementById("voiceCoreTitle");
    const caption = document.getElementById("voiceCaption");
    const connectionLabel = document.getElementById("connectionLabel");
    const connectionChip = document.getElementById("voiceConnectionChip");
    const microphoneLabel = document.getElementById("microphoneLabel");
    const coreMicStatus = document.getElementById("coreMicStatus");
    const coreConnectionStatus = document.getElementById("coreConnectionStatus");
    const topState = document.getElementById("voiceTopState");
    const localTime = document.getElementById("voiceLocalTime");
    const screenReaderState = document.getElementById("voiceScreenReaderState");
    const toast = document.getElementById("voiceToast");
    const demoState = document.getElementById("voiceDemoState");
    const startVoiceButton = document.getElementById("startVoice");
    const muteVoiceButton = document.getElementById("muteVoice");
    const stopSpeakingButton = document.getElementById("stopSpeaking");
    const stopVoiceButton = document.getElementById("stopVoice");
    const remoteAudio = document.getElementById("voiceRemoteAudio");
    const conversationFeed = document.getElementById("voiceConversationFeed");
    const conversationEmpty = document.getElementById("voiceConversationEmpty");
    const backdrop = document.getElementById("voicePanelBackdrop");
    const panels = Array.from(document.querySelectorAll(".voice-panel"));
    const panelButtons = Array.from(document.querySelectorAll("[data-panel-target]"));
    const phaseSteps = Array.from(document.querySelectorAll("[data-voice-phase-step]"));
    const cinematicToggle = document.getElementById("voiceCinematicToggle");
    const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    const cinematicStorageKey = "bbai-voice-cinematic";

    const states = {
        idle: {
            title: "Voice Ready",
            caption: "Press Start Voice when you are ready to request microphone permission.",
            connection: "Not Connected",
            chip: "Not Connected",
            microphone: "Not requested",
            announcement: "Voice is disconnected. Microphone access has not been requested."
        },
        unavailable: {
            title: "Safe Standby",
            caption: "Live voice is unavailable. Your text Command Center still works.",
            connection: "Unavailable",
            chip: "Safe standby",
            microphone: "Not requested",
            announcement: "Live voice is unavailable. Microphone access has not been requested."
        },
        requesting_microphone: {
            title: "Microphone Request",
            caption: "Choose Allow to start the voice-only Builder session.",
            connection: "Permission request",
            chip: "Waiting for permission",
            microphone: "Permission requested",
            announcement: "Microphone permission has been requested."
        },
        connecting: {
            title: "Connecting",
            caption: "Connecting a protected voice session.",
            connection: "Connecting",
            chip: "Secure handshake",
            microphone: "Ready",
            announcement: "Connecting a protected voice session."
        },
        connected: {
            title: "Connected",
            caption: "Voice connected. I’m listening for your business question.",
            connection: "Connected",
            chip: "Session active",
            microphone: "Listening",
            announcement: "Voice connected and listening."
        },
        listening: {
            title: "Listening",
            caption: "Ask a business question or say, “What should I do next?”",
            connection: "Connected",
            chip: "Listening",
            microphone: "Listening",
            announcement: "Voice is connected and listening."
        },
        user_speaking: {
            title: "You’re Speaking",
            caption: "I can hear you. Finish your thought when you’re ready.",
            connection: "Connected",
            chip: "Speech detected",
            microphone: "Receiving speech",
            announcement: "Your speech is being received. Raw audio is not saved."
        },
        processing_transcript: {
            title: "Processing",
            caption: "Turning your speech into one secure text request.",
            connection: "Connected",
            chip: "Transcript processing",
            microphone: "Ready",
            announcement: "Completed speech is being converted into one text request."
        },
        thinking: {
            title: "Thinking",
            caption: "Builder is checking your project and choosing the safest next step.",
            connection: "Connected",
            chip: "Builder working",
            microphone: "Ready",
            announcement: "Builder is preparing a response."
        },
        speaking: {
            title: "Builder Speaking",
            caption: "Builder is speaking the response produced by your protected business agent.",
            connection: "Connected",
            chip: "Speaking",
            microphone: "Ready",
            announcement: "Builder is speaking."
        },
        muted: {
            title: "Muted",
            caption: "Your microphone is muted. Unmute when you want to continue.",
            connection: "Connected",
            chip: "Microphone muted",
            microphone: "Muted",
            announcement: "Microphone muted."
        },
        waiting_for_approval: {
            title: "Approval Required",
            caption: "The proposed action is paused until you review it.",
            connection: "Connected",
            chip: "Action paused",
            microphone: "Ready",
            announcement: "Approval is required. No external action has been performed."
        },
        completed: {
            title: "Completed",
            caption: "Demo state only. This does not represent a completed external action.",
            connection: "Demo · completed",
            chip: "Visual state only",
            microphone: "Not active",
            announcement: "Visual completed state. No external action was completed."
        },
        error: {
            title: "Error",
            caption: "Voice stopped safely. Your text workspace remains available.",
            connection: "Error",
            chip: "Stopped safely",
            microphone: "Not active",
            announcement: "Voice stopped safely after an error."
        },
        disconnected: {
            title: "Disconnected",
            caption: "Voice disconnected. Your saved business work is unchanged.",
            connection: "Disconnected",
            chip: "Session closed",
            microphone: "Not active",
            announcement: "Voice disconnected."
        },
        stopped: {
            title: "Session Ended",
            caption: "Voice stopped. Start a new session whenever you are ready.",
            connection: "Not Connected",
            chip: "Session ended",
            microphone: "Stopped",
            announcement: "Voice session ended."
        },
        exiting: {
            title: "Exiting",
            caption: "Returning to the text Command Center.",
            connection: "Closing panel",
            chip: "Session not started",
            microphone: "Not active",
            announcement: "Exiting the Voice Command Center and returning to text mode."
        }
    };

    const livePhaseByState = {
        requesting_microphone: "listening",
        connecting: "listening",
        connected: "listening",
        listening: "listening",
        user_speaking: "listening",
        muted: "listening",
        processing_transcript: "thinking",
        thinking: "thinking",
        speaking: "speaking",
        waiting_for_approval: "approval"
    };

    const livePhaseTitles = {
        listening: "Listening",
        thinking: "Thinking",
        speaking: "Speaking",
        approval: "Approval Required"
    };

    let currentState = "idle";
    let toastTimer = null;
    let exitStarted = false;
    let animationFrame = null;
    let canvasWidth = 0;
    let canvasHeight = 0;
    let particles = [];
    let activePanelTrigger = null;
    let voice = null;
    let muted = false;

    function titleCase(value) {
        return String(value || "").replace(/_/g, " ").replace(/\b\w/g, function (letter) {
            return letter.toUpperCase();
        });
    }

    function setState(nextState, detail) {
        if (!states[nextState]) return;
        currentState = nextState;
        const details = states[nextState];
        const livePhase = livePhaseByState[nextState] || "ready";
        const visibleTitle = livePhaseTitles[livePhase] || details.title || titleCase(nextState);
        body.dataset.voiceState = nextState;
        body.dataset.voicePhase = livePhase;
        if (stateTitle) stateTitle.textContent = visibleTitle;
        if (caption) caption.textContent = detail || details.caption;
        if (connectionLabel) connectionLabel.textContent = details.connection;
        if (connectionChip) connectionChip.lastChild.textContent = " " + details.chip;
        if (microphoneLabel) microphoneLabel.textContent = details.microphone;
        if (coreMicStatus) coreMicStatus.textContent = details.microphone;
        if (coreConnectionStatus) coreConnectionStatus.textContent = details.connection;
        if (topState) topState.textContent = visibleTitle;
        if (screenReaderState) screenReaderState.textContent = details.announcement;
        if (demoState && demoState.value !== nextState) demoState.value = nextState;
        phaseSteps.forEach(function (step) {
            const active = step.dataset.voicePhaseStep === livePhase;
            step.classList.toggle("is-active", active);
            if (active) step.setAttribute("aria-current", "step");
            else step.removeAttribute("aria-current");
        });
        syncVoiceControls(nextState);
        drawStaticFrame();
    }

    function showToast(message) {
        if (!toast) return;
        window.clearTimeout(toastTimer);
        toast.textContent = message;
        toast.hidden = false;
        toastTimer = window.setTimeout(function () {
            toast.hidden = true;
        }, 4200);
    }

    function voiceAvailable() {
        return Boolean(config.voiceRuntimeEnabled && config.voicePreferenceEnabled);
    }

    function syncVoiceControls(state) {
        const active = ["connected", "listening", "user_speaking", "processing_transcript", "thinking", "speaking", "muted", "waiting_for_approval"].includes(state);
        const liveActive = active && Boolean(voice && voice.peerConnection);
        if (startVoiceButton) {
            startVoiceButton.disabled = liveActive || !voiceAvailable() || state === "connecting" || state === "requesting_microphone";
            startVoiceButton.classList.toggle("is-disabled", startVoiceButton.disabled);
        }
        [muteVoiceButton, stopSpeakingButton, stopVoiceButton].forEach(function (button) {
            if (!button) return;
            button.disabled = !liveActive;
            button.classList.toggle("is-disabled", !liveActive);
            button.setAttribute("aria-disabled", button.disabled ? "true" : "false");
        });
    }

    function appendConversation(role, message) {
        if (!conversationFeed || !message) return;
        if (conversationEmpty) conversationEmpty.remove();
        const entry = document.createElement("article");
        entry.className = "voice-conversation-entry voice-conversation-" + role;
        const label = document.createElement("small");
        label.textContent = role === "user" ? "You said" : "Builder";
        const text = document.createElement("p");
        text.textContent = message;
        entry.append(label, text);
        conversationFeed.appendChild(entry);
        conversationFeed.scrollTop = conversationFeed.scrollHeight;
    }

    function ensureVoice() {
        if (!voice && window.BusinessBuilderRealtimeVoice) {
            voice = new window.BusinessBuilderRealtimeVoice({
                remoteAudio: remoteAudio,
                onState: setState,
                onTranscript: function (message) { appendConversation("user", message); },
                onCanonicalResponse: function (payload) {
                    appendConversation("assistant", payload.reply || "");
                },
                onDuration: function (seconds) {
                    const formatted = String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
                    document.getElementById("voiceDuration")?.replaceChildren(formatted);
                    document.getElementById("voiceTopDuration")?.replaceChildren(formatted);
                }
            });
        }
        return voice;
    }

    async function exitVoicePanel() {
        if (exitStarted) return;
        exitStarted = true;
        closePanels();
        if (voice) await voice.stop("user_exited_voice_panel", true);
        setState("exiting");
        window.setTimeout(function () {
            window.location.assign("/command-center");
        }, body.dataset.motionLevel === "none" || reduceMotionQuery.matches ? 20 : 430);
    }

    function setPanelAccessibility(panel, isOpen) {
        const closeButton = panel.querySelector("[data-close-panel]");
        panel.setAttribute("aria-hidden", isOpen ? "false" : "true");
        panel.toggleAttribute("inert", !isOpen);
        if (isOpen) {
            panel.setAttribute("role", "dialog");
            panel.setAttribute("aria-modal", "true");
            if (closeButton) closeButton.removeAttribute("tabindex");
        } else {
            panel.removeAttribute("role");
            panel.removeAttribute("aria-modal");
            if (closeButton) closeButton.setAttribute("tabindex", "-1");
        }
    }

    function syncDrawerLayout() {
        const openPanelElement = panels.find(function (panel) { return panel.classList.contains("is-open"); });
        panels.forEach(function (panel) { setPanelAccessibility(panel, panel === openPanelElement); });
        body.classList.toggle("voice-drawer-open", Boolean(openPanelElement));
        if (backdrop) backdrop.hidden = !openPanelElement;
    }

    function closePanels(restoreFocus) {
        panels.forEach(function (panel) { panel.classList.remove("is-open"); });
        panelButtons.forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
        panels.forEach(function (panel) { setPanelAccessibility(panel, false); });
        body.classList.remove("voice-drawer-open");
        if (backdrop) backdrop.hidden = true;
        if (restoreFocus && activePanelTrigger?.isConnected) activePanelTrigger.focus();
        activePanelTrigger = null;
    }

    function openPanel(panelId, button) {
        const panel = document.getElementById(panelId);
        if (!panel) return;
        closePanels(false);
        activePanelTrigger = button;
        panel.classList.add("is-open");
        button.setAttribute("aria-expanded", "true");
        setPanelAccessibility(panel, true);
        body.classList.add("voice-drawer-open");
        if (backdrop) backdrop.hidden = false;
        const closeButton = panel.querySelector("[data-close-panel]");
        if (closeButton) closeButton.focus();
    }

    function readCinematicPreference() {
        try {
            return window.localStorage.getItem(cinematicStorageKey) === "on";
        } catch (error) {
            return false;
        }
    }

    function setCinematicMode(enabled, announce) {
        const cinematicEnabled = Boolean(enabled);
        body.classList.toggle("voice-cinematic", cinematicEnabled);
        body.dataset.cinematicMode = cinematicEnabled ? "on" : "off";
        if (cinematicToggle) {
            cinematicToggle.setAttribute("aria-pressed", cinematicEnabled ? "true" : "false");
            const label = cinematicToggle.querySelector("strong");
            if (label) label.textContent = "Cinematic: " + (cinematicEnabled ? "On" : "Off");
        }
        try {
            window.localStorage.setItem(cinematicStorageKey, cinematicEnabled ? "on" : "off");
        } catch (error) {}
        resizeCanvas();
        updateAnimationPreference();
        if (announce) {
            showToast(
                cinematicEnabled
                    ? "Cinematic mode enabled. Voice safety and approvals are unchanged."
                    : "Focus mode restored. Voice safety and approvals are unchanged."
            );
        }
    }

    function resizeCanvas() {
        if (!canvas || !context) return;
        const bounds = canvas.getBoundingClientRect();
        const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
        canvasWidth = Math.max(1, Math.round(bounds.width));
        canvasHeight = Math.max(1, Math.round(bounds.height));
        canvas.width = Math.round(canvasWidth * pixelRatio);
        canvas.height = Math.round(canvasHeight * pixelRatio);
        context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
        particles = Array.from({ length: 34 }, function (_, index) {
            return {
                angle: (Math.PI * 2 * index) / 34,
                radius: .26 + (index % 7) * .036,
                size: index % 5 === 0 ? 1.4 : .75,
                speed: .00008 + (index % 6) * .000012
            };
        });
        drawStaticFrame();
    }

    function stateColor() {
        if (currentState === "error") return [255, 94, 109];
        if (currentState === "waiting_for_approval") return [255, 189, 74];
        if (currentState === "completed") return [85, 230, 177];
        if (currentState === "muted") return [154, 168, 181];
        return [92, 226, 255];
    }

    function drawFrame(time) {
        if (!context || !canvasWidth || !canvasHeight) return;
        const centerX = canvasWidth / 2;
        const centerY = canvasHeight / 2;
        const baseRadius = Math.min(canvasWidth, canvasHeight);
        const color = stateColor();
        context.clearRect(0, 0, canvasWidth, canvasHeight);

        const glow = context.createRadialGradient(centerX, centerY, baseRadius * .08, centerX, centerY, baseRadius * .48);
        glow.addColorStop(0, "rgba(" + color.join(",") + ",.08)");
        glow.addColorStop(.55, "rgba(" + color.join(",") + ",.025)");
        glow.addColorStop(1, "rgba(" + color.join(",") + ",0)");
        context.fillStyle = glow;
        context.fillRect(0, 0, canvasWidth, canvasHeight);

        particles.forEach(function (particle, index) {
            const animationAllowed = body.dataset.motionLevel === "normal" && !reduceMotionQuery.matches;
            const angle = particle.angle + (animationAllowed ? time * particle.speed : 0);
            const radius = baseRadius * particle.radius;
            const x = centerX + Math.cos(angle) * radius;
            const y = centerY + Math.sin(angle) * radius;
            const pulse = animationAllowed ? .45 + Math.sin(time * .0015 + index) * .2 : .52;
            context.beginPath();
            context.arc(x, y, particle.size, 0, Math.PI * 2);
            context.fillStyle = "rgba(" + color.join(",") + "," + pulse + ")";
            context.fill();
        });

        context.strokeStyle = "rgba(" + color.join(",") + ",.1)";
        context.lineWidth = 1;
        for (let index = 0; index < 3; index += 1) {
            context.beginPath();
            context.arc(centerX, centerY, baseRadius * (.31 + index * .055), 0, Math.PI * 2);
            context.stroke();
        }
    }

    function drawStaticFrame() {
        drawFrame(0);
    }

    function animate(time) {
        drawFrame(time);
        animationFrame = window.requestAnimationFrame(animate);
    }

    function updateAnimationPreference() {
        if (animationFrame) {
            window.cancelAnimationFrame(animationFrame);
            animationFrame = null;
        }
        const shouldAnimate = body.dataset.motionLevel === "normal" && !reduceMotionQuery.matches;
        if (shouldAnimate) animationFrame = window.requestAnimationFrame(animate);
        else drawStaticFrame();
    }

    function updateGreetingPeriod() {
        const greeting = document.querySelector("#voiceGreeting h1");
        if (!greeting) return;
        const hour = new Date().getHours();
        const period = hour < 12 ? "morning" : (hour < 18 ? "afternoon" : "evening");
        const textNode = greeting.firstChild;
        if (textNode) textNode.textContent = "Good " + period + ", ";
    }

    function updateLocalTime() {
        if (!localTime) return;
        localTime.textContent = new Intl.DateTimeFormat(undefined, {
            hour: "2-digit",
            minute: "2-digit"
        }).format(new Date());
    }

    startVoiceButton?.addEventListener("click", async function () {
        const client = ensureVoice();
        if (!client) {
            setState("error", "Voice controls could not load. Text mode remains available.");
            return;
        }
        await client.start();
    });
    muteVoiceButton?.addEventListener("click", function () {
        const client = ensureVoice();
        if (!client) return;
        muted = !muted;
        client.setMuted(muted);
        muteVoiceButton.setAttribute("aria-pressed", muted ? "true" : "false");
        const label = muteVoiceButton.querySelector("strong");
        if (label) label.textContent = muted ? "Unmute" : "Mute";
    });
    stopSpeakingButton?.addEventListener("click", function () {
        ensureVoice()?.stopSpeaking();
    });
    stopVoiceButton?.addEventListener("click", async function () {
        if (voice) await voice.stop("user_stopped");
        muted = false;
        if (muteVoiceButton) {
            muteVoiceButton.setAttribute("aria-pressed", "false");
            const label = muteVoiceButton.querySelector("strong");
            if (label) label.textContent = "Mute";
        }
    });
    document.getElementById("exitVoiceButton")?.addEventListener("click", exitVoicePanel);
    document.getElementById("exitVoiceSecondary")?.addEventListener("click", exitVoicePanel);

    if (demoState && config.demoEnabled) {
        demoState.addEventListener("change", function () {
            setState(demoState.value);
        });
    }

    panelButtons.forEach(function (button) {
        button.addEventListener("click", function () { openPanel(button.dataset.panelTarget, button); });
    });
    cinematicToggle?.addEventListener("click", function () {
        setCinematicMode(body.dataset.cinematicMode !== "on", true);
    });
    document.querySelectorAll("[data-close-panel]").forEach(function (button) {
        button.addEventListener("click", function () { closePanels(true); });
    });
    backdrop?.addEventListener("click", function () { closePanels(true); });

    document.addEventListener("keydown", function (event) {
        const openPanelElement = panels.find(function (panel) { return panel.classList.contains("is-open"); });
        if (event.key === "Escape") {
            if (openPanelElement) closePanels(true);
            else exitVoicePanel();
            return;
        }
        if (!openPanelElement && event.key.toLowerCase() === "m" && !muteVoiceButton?.disabled) {
            event.preventDefault();
            muteVoiceButton.click();
            return;
        }
        if (!openPanelElement && event.code === "Space" && !stopSpeakingButton?.disabled && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(document.activeElement?.tagName || "")) {
            event.preventDefault();
            stopSpeakingButton.click();
            return;
        }
        if (!openPanelElement && event.key.toLowerCase() === "c" && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(document.activeElement?.tagName || "")) {
            event.preventDefault();
            cinematicToggle?.click();
            return;
        }
        if (event.key !== "Tab" || !openPanelElement) return;
        const focusable = Array.from(openPanelElement.querySelectorAll("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"));
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    window.addEventListener("pageshow", function () {
        exitStarted = false;
        setState(voiceAvailable() ? "idle" : "unavailable");
        body.classList.add("is-booted");
    });
    window.addEventListener("resize", resizeCanvas, { passive: true });
    reduceMotionQuery.addEventListener?.("change", updateAnimationPreference);

    body.dataset.motionLevel = config.motionLevel || body.dataset.motionLevel || "normal";
    setCinematicMode(readCinematicPreference(), false);
    updateGreetingPeriod();
    updateLocalTime();
    window.setInterval(updateLocalTime, 60000);
    syncDrawerLayout();
    resizeCanvas();
    updateAnimationPreference();
    setState(voiceAvailable() ? "idle" : "unavailable");
    window.requestAnimationFrame(function () {
        body.classList.add("is-booted");
        shell?.setAttribute("data-ready", "true");
    });
}());
