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
    const messageInput = document.getElementById("commandMessage");
    const chatStatus = document.getElementById("commandChatStatus");
    const stopAgentButton = document.getElementById("stopAgentButton");
    const canvas = document.getElementById("voiceWaveform");
    const remoteAudio = document.getElementById("voiceRemoteAudio");
    const researchForm = document.getElementById("researchForm");
    const researchQuery = document.getElementById("researchQuery");
    const researchDepth = document.getElementById("researchDepth");
    const researchStatus = document.getElementById("researchStatus");
    const researchResult = document.getElementById("researchResult");
    const researchHistory = document.getElementById("researchHistory");
    const refreshResearchButton = document.getElementById("refreshResearchButton");
    const monitorRuleForm = document.getElementById("monitorRuleForm");
    const monitorType = document.getElementById("monitorType");
    const monitorFrequency = document.getElementById("monitorFrequency");
    const monitorRules = document.getElementById("monitorRules");
    const refreshMonitoringButton = document.getElementById("refreshMonitoringButton");
    const agentAlerts = document.getElementById("agentAlerts");
    const markAllAlertsReadButton = document.getElementById("markAllAlertsReadButton");
    const browserTaskForm = document.getElementById("browserTaskForm");
    const browserObjective = document.getElementById("browserObjective");
    const browserStartUrl = document.getElementById("browserStartUrl");
    const browserAllowedDomain = document.getElementById("browserAllowedDomain");
    const browserTaskStatus = document.getElementById("browserTaskStatus");
    const browserTaskList = document.getElementById("browserTaskList");
    const browserArtifactViewer = document.getElementById("browserArtifactViewer");
    const browserActionTimeline = document.getElementById("browserActionTimeline");
    const refreshBrowserTasksButton = document.getElementById("refreshBrowserTasksButton");
    const cancelBrowserTaskButton = document.getElementById("cancelBrowserTaskButton");
    const visualPreferencesForm = document.getElementById("visualPreferencesForm");
    const resetVisualPreferencesButton = document.getElementById("resetVisualPreferencesButton");
    const visualPreferencesStatus = document.getElementById("visualPreferencesStatus");
    const VisualStateController = window.BusinessBuilderVisualStateController;
    let visualController = null;

    let voice = null;
    let voiceMode = false;
    let muted = false;
    let activeBrowserTaskId = null;
    let browserPollTimer = null;
    let visualStatePollTimer = null;
    let builderMessageInFlight = false;

    try {
        visualController = VisualStateController ? new VisualStateController(config.initialVisualState, config.visualPreferences) : null;
    } catch (error) {
        console.warn("Builder visuals could not start. Text chat remains available.", error);
        visualController = null;
    }

    if (window.BusinessBuilderCore && visualController) {
        try {
            window.BusinessBuilderCore.init(visualController);
        } catch (error) {
            console.warn("Builder core visual failed safely. Text chat remains available.", error);
        }
    }

    function setVisualState(primaryState, label, severity) {
        if (!visualController || !primaryState) return;
        visualController.setState({
            primary_state: primaryState,
            label: label || primaryState.replace(/_/g, " "),
            severity: severity || "info",
            updated_at: new Date().toISOString()
        });
    }

    function applyVisualPayload(payload) {
        if (!payload) return;
        if (visualController && payload.visual_state) {
            visualController.setState(payload.visual_state);
        }
        if (window.BusinessBuilderVisualisations) {
            window.BusinessBuilderVisualisations.update({
                projectMap: payload.project_map,
                toolStatuses: payload.tool_statuses,
                diagnostics: payload.diagnostics,
                productPipeline: payload.product_pipeline,
                alertRadar: payload.alert_radar
            });
        }
        if (payload.preferences) {
            applyVisualPreferences(payload.preferences, false);
        }
    }

    function applyVisualPreferences(preferences, updateForm) {
        if (!preferences) return;
        if (visualController) visualController.setPreferences(preferences);
        document.body.dataset.visualMode = preferences.visual_mode || "balanced";
        document.body.dataset.motionLevel = preferences.motion_level || "normal";
        if (!updateForm || !visualPreferencesForm) return;
        ["visual_mode", "motion_level", "visual_quality"].forEach((name) => {
            const field = visualPreferencesForm.elements[name];
            if (field && preferences[name]) field.value = preferences[name];
        });
        ["show_floating_panels", "show_3d", "show_particles"].forEach((name) => {
            const field = visualPreferencesForm.elements[name];
            if (field) field.checked = Boolean(preferences[name]);
        });
    }

    async function refreshVisualState() {
        if (!config.visualStateUrl) return;
        try {
            const response = await fetch(config.visualStateUrl, {headers: {"Accept": "application/json"}});
            if (!response.ok) return;
            const payload = await response.json();
            applyVisualPayload(payload);
        } catch (error) {
            // Visual refresh is non-critical; text, voice, approvals, and tools must keep working.
        }
    }

    function startVisualStatePolling() {
        if (visualStatePollTimer || !config.visualStateUrl) return;
        visualStatePollTimer = window.setInterval(refreshVisualState, 10000);
    }

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

    function setChatStatus(message, tone) {
        if (!chatStatus) return;
        chatStatus.textContent = message || "";
        chatStatus.dataset.tone = tone || "";
    }

    function createClientRequestId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return `bb-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }

    async function parseJsonResponse(response) {
        const contentType = response.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
            return response.json();
        }
        const text = await response.text();
        return {error: text || "Builder returned an unreadable response."};
    }

    function setComposerBusy(isBusy) {
        const submitButton = form ? form.querySelector("button[type='submit']") : null;
        if (submitButton) {
            submitButton.disabled = isBusy;
            submitButton.textContent = isBusy ? "Builder is thinking..." : (submitButton.dataset.readyLabel || "Send to Builder");
        }
        if (messageInput) {
            messageInput.disabled = isBusy;
            messageInput.setAttribute("aria-busy", isBusy ? "true" : "false");
        }
    }

    async function submitBuilderMessage(message, options) {
        const settings = options || {};
        const cleanMessage = safeText(message).trim();
        if (!form) return;
        if (!cleanMessage) {
            setChatStatus("Type a message before sending.", "warning");
            if (messageInput) messageInput.focus();
            return;
        }
        if (builderMessageInFlight) {
            setChatStatus("Builder is already working on your last message. Please wait a moment.", "warning");
            return;
        }
        builderMessageInFlight = true;
        setComposerBusy(true);
        setChatStatus("Sending to Builder...", "info");
        setVisualState("thinking", "Builder is planning a safe response");
        try {
            addTranscriptMessage("user", cleanMessage);
            const response = await fetch(config.agentMessageUrl || "/api/agent/message", {
                method: "POST",
                credentials: "same-origin",
                headers: {"Accept": "application/json", "Content-Type": "application/json"},
                body: JSON.stringify({
                    message: cleanMessage,
                    conversation_id: form.dataset.conversationId || config.conversationId,
                    mode: settings.mode || "text",
                    request_id: createClientRequestId()
                })
            });
            const payload = await parseJsonResponse(response);
            if (!response.ok) throw new Error(payload.error || "Builder could not reply.");
            if (payload.conversation_id) {
                form.dataset.conversationId = payload.conversation_id;
                config.conversationId = payload.conversation_id;
                if (voice) voice.conversationId = payload.conversation_id;
            }
            addTranscriptMessage("assistant", payload.reply || "I created a safe next step.");
            if (settings.clearInputOnSuccess && messageInput) {
                messageInput.value = "";
            }
            setChatStatus("Builder replied.", "success");
            if (payload.approval_needed) {
                setVisualState("waiting_for_approval", "A draft is waiting for your approval", "warning");
                await loadAlerts();
            } else {
                setVisualState("completed", "Builder response completed", "success");
            }
            await refreshVisualState();
        } catch (error) {
            addTranscriptMessage("assistant", error.message || "Something went wrong. No external action was taken.");
            setChatStatus(error.message || "Builder could not reply. Your message was not sent again.", "error");
            setVisualState("error", "Builder hit a safe error", "error");
        } finally {
            builderMessageInFlight = false;
            setComposerBusy(false);
            if (messageInput) messageInput.focus();
        }
    }

    function safeText(value) {
        return String(value || "");
    }

    function terminalResearchStatus(status) {
        return ["completed", "failed", "cancelled"].includes(status);
    }

    function terminalBrowserStatus(status) {
        return ["completed", "failed", "cancelled", "timed_out", "blocked"].includes(status);
    }

    function renderResearchJob(job) {
        if (!researchResult || !job) return;
        researchStatus.textContent = `Research ${job.status}: ${job.query}`;
        const result = job.result || {};
        const sources = result.sources || [];
        const findings = result.key_findings || [];
        const risks = result.risks || result.uncertainties || [];
        researchResult.replaceChildren();
        const card = document.createElement("div");
        card.className = "research-output-card";
        const title = document.createElement("h3");
        title.textContent = job.query;
        const status = document.createElement("span");
        status.className = "build-status " + job.status;
        status.textContent = job.status;
        const answer = document.createElement("p");
        answer.textContent = safeText(result.answer || job.result_summary || job.error_message || "Research status updated.");
        card.append(status, title, answer);
        if (findings.length) {
            const list = document.createElement("ul");
            findings.slice(0, 8).forEach((item) => {
                const li = document.createElement("li");
                li.textContent = safeText(item);
                list.appendChild(li);
            });
            card.appendChild(list);
        }
        if (result.recommendation) {
            const rec = document.createElement("p");
            const recLabel = document.createElement("strong");
            recLabel.textContent = "Recommendation: ";
            rec.appendChild(recLabel);
            rec.appendChild(document.createTextNode(result.recommendation));
            card.appendChild(rec);
        }
        if (risks.length) {
            const risk = document.createElement("p");
            const riskLabel = document.createElement("strong");
            riskLabel.textContent = "Risks / uncertainty: ";
            risk.appendChild(riskLabel);
            risk.appendChild(document.createTextNode(risks.slice(0, 4).join(" ")));
            card.appendChild(risk);
        }
        if (sources.length) {
            const sourceMap = document.createElement("div");
            sourceMap.className = "research-source-map";
            sourceMap.setAttribute("aria-label", "Research source map");
            sources.slice(0, 10).forEach((source) => {
                const node = document.createElement("span");
                node.dataset.sourceType = source.source_type || "supporting";
                node.textContent = source.domain || source.title || "Source";
                sourceMap.appendChild(node);
            });
            card.appendChild(sourceMap);
            const sourceTitle = document.createElement("h4");
            sourceTitle.textContent = "Sources";
            const sourceList = document.createElement("ul");
            sources.forEach((source) => {
                const li = document.createElement("li");
                const link = document.createElement("a");
                link.href = source.url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.textContent = `[${source.citation_label || "S"}] ${source.title || source.domain || "Source"}`;
                li.appendChild(link);
                sourceList.appendChild(li);
            });
            card.append(sourceTitle, sourceList);
        }
        researchResult.appendChild(card);
    }

    async function loadResearchJobs() {
        if (!researchHistory) return;
        try {
            const response = await fetch(config.researchUrl || "/api/research");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load research.");
            researchHistory.replaceChildren();
            if (!payload.research_jobs.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No research yet.";
                researchHistory.appendChild(empty);
                return;
            }
            payload.research_jobs.slice(0, 8).forEach((job) => {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "research-history-item";
                item.textContent = `${job.status} · ${job.research_type} · ${job.query}`;
                item.addEventListener("click", () => renderResearchJob(job));
                researchHistory.appendChild(item);
            });
        } catch (error) {
            if (researchStatus) researchStatus.textContent = error.message;
        }
    }

    async function pollResearchJob(jobId) {
        const interval = window.setInterval(async () => {
            try {
                const response = await fetch(`${config.researchUrl || "/api/research"}/${jobId}`);
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Could not refresh research.");
                renderResearchJob(payload.research_job);
                if (terminalResearchStatus(payload.research_job.status)) {
                    window.clearInterval(interval);
                    setVisualState(
                        payload.research_job.status === "completed" ? "completed" : "error",
                        payload.research_job.status === "completed" ? "Research completed" : "Research stopped safely",
                        payload.research_job.status === "completed" ? "success" : "warning"
                    );
                    loadResearchJobs();
                    loadAlerts();
                    refreshVisualState();
                }
            } catch (error) {
                window.clearInterval(interval);
                if (researchStatus) researchStatus.textContent = error.message;
                setVisualState("error", "Research could not refresh", "error");
            }
        }, 7000);
    }

    async function loadMonitorRules() {
        if (!monitorRules) return;
        try {
            const response = await fetch(config.monitoringRulesUrl || "/api/monitoring/rules");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load monitor rules.");
            monitorRules.replaceChildren();
            if (!payload.rules.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No monitor rules yet.";
                monitorRules.appendChild(empty);
                return;
            }
            payload.rules.forEach((rule) => {
                const item = document.createElement("div");
                const title = document.createElement("strong");
                title.textContent = rule.name;
                const copy = document.createElement("p");
                copy.textContent = `${rule.enabled ? "Enabled" : "Disabled"} · every ${rule.frequency_minutes} minutes`;
                const toggle = document.createElement("button");
                toggle.type = "button";
                toggle.className = "secondary-button";
                toggle.textContent = rule.enabled ? "Disable" : "Enable";
                toggle.addEventListener("click", async () => {
                    await fetch(`${config.monitoringRulesUrl || "/api/monitoring/rules"}/${rule.id}`, {
                        method: "PATCH",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({enabled: !rule.enabled})
                    });
                    loadMonitorRules();
                });
                item.append(title, copy, toggle);
                monitorRules.appendChild(item);
            });
        } catch (error) {
            monitorRules.textContent = error.message;
        }
    }

    async function loadAlerts() {
        if (!agentAlerts) return;
        try {
            const response = await fetch(config.alertsUrl || "/api/alerts");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load alerts.");
            agentAlerts.replaceChildren();
            if (!payload.alerts.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No alerts yet.";
                agentAlerts.appendChild(empty);
                return;
            }
            payload.alerts.slice(0, 8).forEach((alert) => {
                const item = document.createElement("div");
                const badge = document.createElement("span");
                badge.className = "build-status " + alert.severity;
                badge.textContent = alert.severity;
                const title = document.createElement("strong");
                title.textContent = alert.title;
                const copy = document.createElement("p");
                copy.textContent = alert.message;
                const read = document.createElement("button");
                read.type = "button";
                read.className = "secondary-button";
                read.textContent = alert.read ? "Read" : "Mark read";
                read.disabled = alert.read;
                read.addEventListener("click", async () => {
                    await fetch(`${config.alertsUrl || "/api/alerts"}/${alert.id}/read`, {method: "POST"});
                    loadAlerts();
                });
                item.append(badge, title, copy, read);
                agentAlerts.appendChild(item);
            });
        } catch (error) {
            agentAlerts.textContent = error.message;
        }
    }

    async function loadBrowserActions(taskId) {
        if (!browserActionTimeline || !taskId) return;
        try {
            const response = await fetch(`${config.browserTasksUrl || "/api/browser/tasks"}/${taskId}/actions`);
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load browser actions.");
            browserActionTimeline.replaceChildren();
            if (!payload.actions.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No browser actions recorded yet.";
                browserActionTimeline.appendChild(empty);
                return;
            }
            payload.actions.forEach((action) => {
                const item = document.createElement("div");
                const badge = document.createElement("span");
                badge.className = `build-status ${action.status}`;
                badge.textContent = action.status;
                const title = document.createElement("strong");
                title.textContent = `${action.sequence_number}. ${action.action_type}`;
                const copy = document.createElement("p");
                copy.textContent = action.action_summary || action.validation.reason || "Browser action recorded.";
                item.append(badge, title, copy);
                browserActionTimeline.appendChild(item);
            });
        } catch (error) {
            browserActionTimeline.textContent = error.message;
        }
    }

    async function loadBrowserArtifacts(taskId) {
        if (!browserArtifactViewer || !taskId) return;
        try {
            const response = await fetch(`${config.browserTasksUrl || "/api/browser/tasks"}/${taskId}/artifacts`);
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load browser screenshots.");
            browserArtifactViewer.replaceChildren();
            if (!payload.artifacts.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No screenshots yet.";
                browserArtifactViewer.appendChild(empty);
                return;
            }
            payload.artifacts.slice(-4).forEach((artifact) => {
                const figure = document.createElement("figure");
                const img = document.createElement("img");
                img.src = artifact.url;
                img.alt = `${artifact.artifact_type} ${artifact.sequence_number}`;
                img.loading = "lazy";
                const caption = document.createElement("figcaption");
                caption.textContent = `${artifact.artifact_type} · expires ${artifact.expires_at || "soon"}`;
                figure.append(img, caption);
                browserArtifactViewer.appendChild(figure);
            });
        } catch (error) {
            browserArtifactViewer.textContent = error.message;
        }
    }

    async function loadBrowserTasks() {
        if (!browserTaskList) return;
        try {
            const response = await fetch(config.browserTasksUrl || "/api/browser/tasks");
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "Could not load browser tasks.");
            browserTaskList.replaceChildren();
            const tasks = payload.browser_tasks || [];
            if (!tasks.length) {
                const empty = document.createElement("p");
                empty.className = "empty-card";
                empty.textContent = "No browser tasks yet.";
                browserTaskList.appendChild(empty);
                if (browserTaskStatus) browserTaskStatus.textContent = config.browserControlEnabled ? "Ready for a safe browser task." : "Browser control is disabled until configured.";
                return;
            }
            const active = tasks.find((task) => !terminalBrowserStatus(task.status));
            activeBrowserTaskId = active ? active.id : tasks[0].id;
            if (active) {
                setVisualState("browser_running", "Restricted browser inspection is active", "info");
            }
            if (cancelBrowserTaskButton) cancelBrowserTaskButton.disabled = !active;
            tasks.slice(0, 8).forEach((task) => {
                const item = document.createElement("button");
                item.type = "button";
                item.className = "research-history-item browser-task-item";
                item.textContent = `${task.status} · ${task.objective}`;
                item.addEventListener("click", () => {
                    activeBrowserTaskId = task.id;
                    loadBrowserActions(task.id);
                    loadBrowserArtifacts(task.id);
                    if (browserTaskStatus) browserTaskStatus.textContent = task.final_summary || task.current_step || `Browser task ${task.status}.`;
                });
                browserTaskList.appendChild(item);
            });
            const selected = tasks.find((task) => task.id === activeBrowserTaskId) || tasks[0];
            if (browserTaskStatus) browserTaskStatus.textContent = selected.final_summary || selected.current_step || `Browser task ${selected.status}.`;
            loadBrowserActions(selected.id);
            loadBrowserArtifacts(selected.id);
            if (active && !browserPollTimer) {
                browserPollTimer = window.setInterval(async () => {
                    await loadBrowserTasks();
                    const detail = await fetch(`${config.browserTasksUrl || "/api/browser/tasks"}/${active.id}`).then((r) => r.json()).catch(() => null);
                    if (!detail || !detail.browser_task || terminalBrowserStatus(detail.browser_task.status)) {
                        window.clearInterval(browserPollTimer);
                        browserPollTimer = null;
                        loadAlerts();
                        refreshVisualState();
                    }
                }, 5000);
            }
        } catch (error) {
            if (browserTaskStatus) browserTaskStatus.textContent = error.message;
            setVisualState("error", "Browser task status could not load", "error");
        }
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
        const voiceVisualMap = {
            connected: "listening",
            listening: "listening",
            user_speaking: "user_speaking",
            processing_transcript: "thinking",
            thinking: "thinking",
            waiting_for_approval: "waiting_for_approval",
            speaking: "speaking",
            muted: "muted",
            idle: "idle"
        };
        if (voiceVisualMap[state]) {
            setVisualState(
                voiceVisualMap[state],
                detail || `Voice mode: ${state.replace(/_/g, " ")}`,
                state === "waiting_for_approval" ? "warning" : "info"
            );
        }
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
                    setVisualState(payload.approval_needed ? "waiting_for_approval" : "completed", payload.approval_needed ? "Voice draft is waiting for approval" : "Voice response completed", payload.approval_needed ? "warning" : "success");
                    refreshVisualState();
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

    if (form && form.dataset.builderSubmitBound !== "true") {
        form.dataset.builderSubmitBound = "true";
        const submitButton = form.querySelector("button[type='submit']");
        if (submitButton && !submitButton.dataset.readyLabel) {
            submitButton.dataset.readyLabel = submitButton.textContent || "Send to Builder";
        }
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const message = messageInput ? messageInput.value.trim() : "";
            await submitBuilderMessage(message, {mode: "text", clearInputOnSuccess: true});
        });
    }

    if (messageInput && messageInput.dataset.builderKeyBound !== "true") {
        messageInput.dataset.builderKeyBound = "true";
        messageInput.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) {
                return;
            }
            event.preventDefault();
            if (form && typeof form.requestSubmit === "function") {
                form.requestSubmit();
            } else if (form) {
                form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
            }
        });
    }

    if (stopAgentButton) {
        stopAgentButton.addEventListener("click", async () => {
            try {
                const response = await fetch(config.agentStopUrl || "/api/agent/stop", {method: "POST"});
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Could not stop Builder.");
                addTranscriptMessage("assistant", payload.message || "Stopped safely. No external action was taken.");
                setVisualState("idle", "Builder stopped safely");
            } catch (error) {
                addTranscriptMessage("assistant", error.message || "Could not stop the current response.");
            }
        });
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

    if (researchForm) {
        researchForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const query = researchQuery.value.trim();
            if (!query) return;
            researchStatus.textContent = "Creating research plan...";
            researchResult.replaceChildren();
            setVisualState("researching", "Business research is running");
            try {
                const response = await fetch(config.researchUrl || "/api/research", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({query, research_type: researchDepth.value})
                });
                const payload = await response.json();
                if (!response.ok && !payload.research_job) throw new Error(payload.error || "Research failed.");
                renderResearchJob(payload.research_job);
                loadResearchJobs();
                if (payload.queued || !terminalResearchStatus(payload.research_job.status)) {
                    pollResearchJob(payload.research_job.id);
                } else {
                    setVisualState("completed", "Research completed", "success");
                    refreshVisualState();
                }
            } catch (error) {
                researchStatus.textContent = error.message;
                setVisualState("error", "Research failed safely", "error");
            }
        });
    }

    if (refreshResearchButton) refreshResearchButton.addEventListener("click", loadResearchJobs);

    if (monitorRuleForm) {
        monitorRuleForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            await fetch(config.monitoringRulesUrl || "/api/monitoring/rules", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    monitor_type: monitorType.value,
                    frequency_minutes: Number(monitorFrequency.value),
                    enabled: true
                })
            });
            loadMonitorRules();
        });
    }

    if (refreshMonitoringButton) refreshMonitoringButton.addEventListener("click", () => { loadMonitorRules(); loadAlerts(); });
    if (markAllAlertsReadButton) {
        markAllAlertsReadButton.addEventListener("click", async () => {
            await fetch("/api/alerts/read-all", {method: "POST"});
            loadAlerts();
        });
    }

    if (browserTaskForm) {
        browserTaskForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const objective = browserObjective.value.trim();
            const startUrl = browserStartUrl.value.trim();
            const allowedDomain = browserAllowedDomain.value.trim();
            if (!objective || !startUrl) return;
            if (browserTaskStatus) browserTaskStatus.textContent = "Creating safe browser task...";
            setVisualState("browser_running", "Creating a restricted browser inspection");
            try {
                const response = await fetch(config.browserTasksUrl || "/api/browser/tasks", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        objective,
                        start_url: startUrl,
                        allowed_domains: allowedDomain ? [allowedDomain] : []
                    })
                });
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Could not create browser task.");
                activeBrowserTaskId = payload.browser_task.id;
                if (browserTaskStatus) browserTaskStatus.textContent = "Browser task queued for the isolated worker.";
                await loadBrowserTasks();
                await refreshVisualState();
            } catch (error) {
                if (browserTaskStatus) browserTaskStatus.textContent = error.message;
                setVisualState("error", "Browser task was blocked safely", "error");
            }
        });
    }

    if (refreshBrowserTasksButton) refreshBrowserTasksButton.addEventListener("click", loadBrowserTasks);

    if (cancelBrowserTaskButton) {
        cancelBrowserTaskButton.addEventListener("click", async () => {
            if (!activeBrowserTaskId) return;
            try {
                const response = await fetch(`${config.browserTasksUrl || "/api/browser/tasks"}/${activeBrowserTaskId}/cancel`, {method: "POST"});
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Could not cancel browser task.");
                if (browserTaskStatus) browserTaskStatus.textContent = "Browser task cancelled.";
                await loadBrowserTasks();
                await refreshVisualState();
            } catch (error) {
                if (browserTaskStatus) browserTaskStatus.textContent = error.message;
            }
        });
    }

    if (visualPreferencesForm) {
        visualPreferencesForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const formData = new FormData(visualPreferencesForm);
            const payload = {
                visual_mode: formData.get("visual_mode"),
                motion_level: formData.get("motion_level"),
                visual_quality: formData.get("visual_quality"),
                show_floating_panels: formData.has("show_floating_panels"),
                show_3d: formData.has("show_3d"),
                show_particles: formData.has("show_particles")
            };
            if (visualPreferencesStatus) visualPreferencesStatus.textContent = "Saving visual preferences...";
            try {
                const response = await fetch(config.visualPreferencesUrl || "/api/visual/preferences", {
                    method: "PATCH",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload)
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error || "Could not save visual preferences.");
                applyVisualPreferences(result.preferences, true);
                if (visualPreferencesStatus) visualPreferencesStatus.textContent = "Visual preferences saved.";
            } catch (error) {
                if (visualPreferencesStatus) visualPreferencesStatus.textContent = error.message;
            }
        });
    }

    if (resetVisualPreferencesButton) {
        resetVisualPreferencesButton.addEventListener("click", async () => {
            if (visualPreferencesStatus) visualPreferencesStatus.textContent = "Resetting visual preferences...";
            try {
                const response = await fetch(config.visualPreferencesUrl || "/api/visual/preferences", {
                    method: "PATCH",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({reset: true})
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error || "Could not reset visual preferences.");
                applyVisualPreferences(result.preferences, true);
                if (visualPreferencesStatus) visualPreferencesStatus.textContent = "Visual preferences reset.";
            } catch (error) {
                if (visualPreferencesStatus) visualPreferencesStatus.textContent = error.message;
            }
        });
    }

    window.addEventListener("beforeunload", () => {
        if (voice) voice.stop("page_unload");
        if (visualStatePollTimer) window.clearInterval(visualStatePollTimer);
    });

    document.addEventListener("visibilitychange", () => {
        if (document.hidden && voiceCaption && voiceMode) {
            voiceCaption.textContent = "Tab is in the background. Voice remains connected only while your browser allows it.";
        }
    });

    setMode("text");
    setVoiceState("idle", "Voice is idle. Click Voice, then Start Voice when you are ready.");
    loadResearchJobs();
    loadMonitorRules();
    loadAlerts();
    loadBrowserTasks();
    applyVisualPreferences(config.visualPreferences, true);
    applyVisualPayload({
        visual_state: config.initialVisualState,
        project_map: config.initialProjectMap,
        tool_statuses: config.initialToolStatuses,
        diagnostics: config.initialDiagnostics,
        product_pipeline: config.initialProductPipeline,
        alert_radar: config.initialAlertRadar,
        preferences: config.visualPreferences
    });
    refreshVisualState();
    startVisualStatePolling();
})();
