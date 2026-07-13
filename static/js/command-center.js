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

    let voice = null;
    let voiceMode = false;
    let muted = false;
    let activeBrowserTaskId = null;
    let browserPollTimer = null;

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
            rec.innerHTML = "<strong>Recommendation:</strong> ";
            rec.appendChild(document.createTextNode(result.recommendation));
            card.appendChild(rec);
        }
        if (risks.length) {
            const risk = document.createElement("p");
            risk.innerHTML = "<strong>Risks / uncertainty:</strong> ";
            risk.appendChild(document.createTextNode(risks.slice(0, 4).join(" ")));
            card.appendChild(risk);
        }
        if (sources.length) {
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
                    loadResearchJobs();
                    loadAlerts();
                }
            } catch (error) {
                window.clearInterval(interval);
                if (researchStatus) researchStatus.textContent = error.message;
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
                    }
                }, 5000);
            }
        } catch (error) {
            if (browserTaskStatus) browserTaskStatus.textContent = error.message;
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

    if (researchForm) {
        researchForm.addEventListener("submit", async (event) => {
            event.preventDefault();
            const query = researchQuery.value.trim();
            if (!query) return;
            researchStatus.textContent = "Creating research plan...";
            researchResult.replaceChildren();
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
                }
            } catch (error) {
                researchStatus.textContent = error.message;
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
            } catch (error) {
                if (browserTaskStatus) browserTaskStatus.textContent = error.message;
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
            } catch (error) {
                if (browserTaskStatus) browserTaskStatus.textContent = error.message;
            }
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
    loadResearchJobs();
    loadMonitorRules();
    loadAlerts();
    loadBrowserTasks();
})();
