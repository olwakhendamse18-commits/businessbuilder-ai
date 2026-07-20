(function () {
    "use strict";

    const PRIORITY = {
        error: 100,
        warning: 90,
        waiting_for_approval: 85,
        waiting_for_user: 80,
        user_speaking: 75,
        speaking: 74,
        listening: 72,
        browser_running: 65,
        tool_running: 62,
        researching: 60,
        planning: 50,
        thinking: 48,
        completed: 30,
        idle: 10,
        offline: 5,
        muted: 4
    };

    function normalize(state) {
        const now = new Date().toISOString();
        const source = state || {};
        return {
            primary_state: source.primary_state || "idle",
            secondary_state: source.secondary_state || "",
            label: source.label || "Builder is ready",
            severity: source.severity || "info",
            progress_type: source.progress_type || "none",
            launch_progress: Number.isFinite(Number(source.launch_progress)) ? Number(source.launch_progress) : null,
            active_tool: source.active_tool || "",
            approval_required: Boolean(source.approval_required),
            pending_approval_count: Number(source.pending_approval_count || 0),
            active_research_count: Number(source.active_research_count || 0),
            active_browser_count: Number(source.active_browser_count || 0),
            updated_at: source.updated_at || now
        };
    }

    class VisualStateController extends EventTarget {
        constructor(initialState, preferences) {
            super();
            this.preferences = preferences || {};
            this.state = normalize(initialState);
            this.stateRank = PRIORITY[this.state.primary_state] || 0;
            this.updatedAt = Date.parse(this.state.updated_at) || Date.now();
            this.apply();
        }

        update(nextState, options) {
            const normalized = normalize(nextState);
            const incomingTime = Date.parse(normalized.updated_at) || Date.now();
            const incomingRank = PRIORITY[normalized.primary_state] || 0;
            const force = options && options.force;
            const stale = incomingTime + 1500 < this.updatedAt && incomingRank < this.stateRank;
            if (!force && stale) return this.state;
            this.state = normalized;
            this.stateRank = incomingRank;
            this.updatedAt = Math.max(this.updatedAt, incomingTime);
            this.apply();
            this.dispatchEvent(new CustomEvent("statechange", {detail: this.state}));
            return this.state;
        }

        setPreferences(preferences) {
            this.preferences = preferences || {};
            document.body.dataset.visualMode = this.preferences.visual_mode || "balanced";
            document.body.dataset.motionLevel = this.preferences.motion_level || "normal";
            this.dispatchEvent(new CustomEvent("preferenceschange", {detail: this.preferences}));
        }

        apply() {
            document.body.dataset.commandState = this.state.primary_state;
            const label = document.getElementById("agentStateLabel");
            const coreLabel = document.getElementById("builderCoreLabel");
            const coreDetail = document.getElementById("builderCoreDetail");
            const shell = document.getElementById("builderCoreShell");
            if (label) label.textContent = this.state.label;
            if (coreLabel) coreLabel.textContent = this.state.label;
            if (coreDetail) coreDetail.textContent = `State: ${this.state.primary_state.replace(/_/g, " ")}`;
            if (shell) shell.dataset.state = this.state.primary_state;
        }
    }

    window.BusinessBuilderVisualStateController = VisualStateController;
})();
