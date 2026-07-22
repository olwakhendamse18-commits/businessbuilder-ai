(function () {
    "use strict";

    const config = window.BB_VOICE_PROTOTYPE || {};
    const body = document.body;
    const shell = document.getElementById("voiceShell");
    const canvas = document.getElementById("voiceCoreCanvas");
    const context = canvas ? canvas.getContext("2d") : null;
    const stateTitle = document.getElementById("voiceCoreTitle");
    const caption = document.getElementById("voiceCaption");
    const connectionLabel = document.getElementById("connectionLabel");
    const connectionChip = document.getElementById("voiceConnectionChip");
    const microphoneLabel = document.getElementById("microphoneLabel");
    const screenReaderState = document.getElementById("voiceScreenReaderState");
    const toast = document.getElementById("voiceToast");
    const demoState = document.getElementById("voiceDemoState");
    const backdrop = document.getElementById("voicePanelBackdrop");
    const panels = Array.from(document.querySelectorAll(".voice-panel"));
    const panelButtons = Array.from(document.querySelectorAll("[data-panel-target]"));
    const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

    const states = {
        idle: {
            title: "Idle",
            caption: "Voice connection will be enabled in Phase 2.",
            connection: "Prototype ready",
            chip: "Offline prototype",
            microphone: "Not requested",
            announcement: "Voice prototype is idle. Microphone access has not been requested."
        },
        requesting_microphone: {
            title: "Microphone Request",
            caption: "Demo state only. Phase 1 never requests microphone permission.",
            connection: "Demo · permission step",
            chip: "No permission request sent",
            microphone: "Demo state",
            announcement: "Visual demonstration of a future microphone permission request. No request was sent."
        },
        connecting: {
            title: "Connecting",
            caption: "Demo state only. No Realtime session or network request is active.",
            connection: "Demo · connecting",
            chip: "No network session",
            microphone: "Not connected",
            announcement: "Visual demonstration of a future connection state. No network session exists."
        },
        listening: {
            title: "Listening",
            caption: "Demo state only. No microphone stream is being captured.",
            connection: "Demo · listening",
            chip: "No audio capture",
            microphone: "Demo state",
            announcement: "Visual listening demonstration. No audio is being captured."
        },
        user_speaking: {
            title: "You’re Speaking",
            caption: "Demo waveform only. No raw audio exists or is stored.",
            connection: "Demo · user speaking",
            chip: "Visual simulation",
            microphone: "No audio capture",
            announcement: "Visual user speaking demonstration. No raw audio exists."
        },
        thinking: {
            title: "Thinking",
            caption: "Demo state only. No AI model request has been made.",
            connection: "Demo · processing",
            chip: "No model request",
            microphone: "Not active",
            announcement: "Visual thinking demonstration. No AI model request was made."
        },
        speaking: {
            title: "Builder Speaking",
            caption: "Demo state only. No synthesized voice or remote audio is playing.",
            connection: "Demo · speaking",
            chip: "No audio playback",
            microphone: "Not active",
            announcement: "Visual Builder speaking demonstration. No audio is playing."
        },
        muted: {
            title: "Muted",
            caption: "Demo state only. Phase 1 has no microphone stream to mute.",
            connection: "Demo · muted",
            chip: "Prototype muted",
            microphone: "No stream",
            announcement: "Visual muted demonstration. There is no microphone stream."
        },
        waiting_for_approval: {
            title: "Approval Required",
            caption: "A future voice action would pause here until you explicitly approve it.",
            connection: "Demo · approval gate",
            chip: "Action paused",
            microphone: "Not active",
            announcement: "Visual approval gate. No external action has been performed."
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
            caption: "Demo error state. The prototype remains safe and disconnected.",
            connection: "Demo · error",
            chip: "Prototype error",
            microphone: "Not active",
            announcement: "Visual error state. The prototype remains disconnected."
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

    let currentState = "idle";
    let toastTimer = null;
    let exitStarted = false;
    let animationFrame = null;
    let canvasWidth = 0;
    let canvasHeight = 0;
    let particles = [];

    function titleCase(value) {
        return String(value || "").replace(/_/g, " ").replace(/\b\w/g, function (letter) {
            return letter.toUpperCase();
        });
    }

    function setState(nextState) {
        if (!states[nextState]) return;
        currentState = nextState;
        const details = states[nextState];
        body.dataset.voiceState = nextState;
        if (stateTitle) stateTitle.textContent = details.title || titleCase(nextState);
        if (caption) caption.textContent = details.caption;
        if (connectionLabel) connectionLabel.textContent = details.connection;
        if (connectionChip) connectionChip.lastChild.textContent = " " + details.chip;
        if (microphoneLabel) microphoneLabel.textContent = details.microphone;
        if (screenReaderState) screenReaderState.textContent = details.announcement;
        if (demoState && demoState.value !== nextState) demoState.value = nextState;
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

    function prototypeOnly(controlName) {
        setState("idle");
        showToast(controlName + " will be enabled in Phase 2. No microphone, audio, AI, or network connection was started.");
    }

    function exitVoicePanel() {
        if (exitStarted) return;
        exitStarted = true;
        closePanels();
        setState("exiting");
        window.setTimeout(function () {
            window.location.assign("/command-center");
        }, body.dataset.motionLevel === "none" || reduceMotionQuery.matches ? 20 : 430);
    }

    function closePanels() {
        panels.forEach(function (panel) { panel.classList.remove("is-open"); });
        panelButtons.forEach(function (button) { button.setAttribute("aria-expanded", "false"); });
        if (backdrop) backdrop.hidden = true;
    }

    function openPanel(panelId, button) {
        const panel = document.getElementById(panelId);
        if (!panel) return;
        closePanels();
        panel.classList.add("is-open");
        button.setAttribute("aria-expanded", "true");
        if (backdrop) backdrop.hidden = false;
        const closeButton = panel.querySelector("[data-close-panel]");
        if (closeButton) closeButton.focus();
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

    document.getElementById("startVoicePrototype")?.addEventListener("click", function () {
        prototypeOnly("Voice connection");
    });
    document.getElementById("muteVoicePrototype")?.addEventListener("click", function () {
        prototypeOnly("Mute control");
    });
    document.getElementById("stopSpeakingPrototype")?.addEventListener("click", function () {
        prototypeOnly("Stop Speaking");
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
    document.querySelectorAll("[data-close-panel]").forEach(function (button) {
        button.addEventListener("click", closePanels);
    });
    backdrop?.addEventListener("click", closePanels);

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        if (panels.some(function (panel) { return panel.classList.contains("is-open"); })) closePanels();
        else exitVoicePanel();
    });

    window.addEventListener("pageshow", function () {
        exitStarted = false;
        setState("idle");
        body.classList.add("is-booted");
    });
    window.addEventListener("resize", resizeCanvas, { passive: true });
    reduceMotionQuery.addEventListener?.("change", updateAnimationPreference);

    body.dataset.motionLevel = config.motionLevel || body.dataset.motionLevel || "normal";
    updateGreetingPeriod();
    resizeCanvas();
    updateAnimationPreference();
    setState("idle");
    window.requestAnimationFrame(function () {
        body.classList.add("is-booted");
        shell?.setAttribute("data-ready", "true");
    });
}());
