(function () {
    "use strict";

    class BuilderCore2D {
        constructor(canvas, stateController) {
            this.canvas = canvas;
            this.ctx = canvas ? canvas.getContext("2d") : null;
            this.stateController = stateController;
            this.running = false;
            this.visible = true;
            this.frame = 0;
            this.raf = null;
            this.resize = this.resize.bind(this);
            this.draw = this.draw.bind(this);
            this.handleVisibilityChange = this.handleVisibilityChange.bind(this);
        }

        handleVisibilityChange() {
            this.visible = !document.hidden;
        }

        start() {
            if (!this.canvas || !this.ctx || this.running) return;
            this.running = true;
            this.resize();
            window.addEventListener("resize", this.resize, {passive: true});
            document.addEventListener("visibilitychange", this.handleVisibilityChange);
            this.draw();
        }

        stop() {
            this.running = false;
            if (this.raf) window.cancelAnimationFrame(this.raf);
            window.removeEventListener("resize", this.resize);
            document.removeEventListener("visibilitychange", this.handleVisibilityChange);
        }

        resize() {
            const perf = window.BusinessBuilderVisualPerformance;
            const profile = perf ? perf.getProfile(this.stateController.preferences) : {pixelRatio: 1};
            const rect = this.canvas.getBoundingClientRect();
            const size = Math.min(460, Math.max(260, rect.width || 360));
            this.canvas.width = Math.floor(size * profile.pixelRatio);
            this.canvas.height = Math.floor(size * profile.pixelRatio);
            this.canvas.style.height = `${size}px`;
            this.canvas.style.width = `${size}px`;
            this.ctx.setTransform(profile.pixelRatio, 0, 0, profile.pixelRatio, 0, 0);
        }

        colors(state) {
            if (state.includes("approval") || state.includes("warning") || state.includes("user")) {
                return {main: "#fb923c", glow: "rgba(251, 146, 60, .24)"};
            }
            if (state === "error") return {main: "#fb7185", glow: "rgba(251, 113, 133, .22)"};
            if (state.includes("research") || state.includes("browser") || state.includes("tool")) {
                return {main: "#38bdf8", glow: "rgba(56, 189, 248, .2)"};
            }
            if (state.includes("listening") || state.includes("speaking")) {
                return {main: "#2dd4bf", glow: "rgba(45, 212, 191, .23)"};
            }
            return {main: "#67e8f9", glow: "rgba(103, 232, 249, .18)"};
        }

        draw() {
            if (!this.running) return;
            const ctx = this.ctx;
            const width = this.canvas.clientWidth || 360;
            const height = this.canvas.clientHeight || 360;
            const state = this.stateController.state.primary_state;
            const perf = window.BusinessBuilderVisualPerformance;
            const profile = perf ? perf.getProfile(this.stateController.preferences) : {noMotion: false, reducedMotion: false, particles: true};
            const animate = this.visible && !profile.noMotion;
            if (animate) this.frame += profile.reducedMotion ? 0.35 : 1;
            ctx.clearRect(0, 0, width, height);
            const cx = width / 2;
            const cy = height / 2;
            const base = Math.min(width, height) * 0.31;
            const palette = this.colors(state);

            ctx.save();
            ctx.shadowColor = palette.glow;
            ctx.shadowBlur = 28;
            ctx.fillStyle = "rgba(15, 23, 42, .78)";
            ctx.beginPath();
            ctx.arc(cx, cy, base * 0.74, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();

            for (let ring = 0; ring < 3; ring += 1) {
                const radius = base + ring * 28;
                const start = (this.frame / (80 + ring * 20)) + ring;
                ctx.lineWidth = ring === 0 ? 4 : 2;
                ctx.strokeStyle = ring === 0 ? palette.main : "rgba(147, 197, 253, .32)";
                ctx.beginPath();
                ctx.arc(cx, cy, radius, start, start + Math.PI * (1.1 + ring * 0.18));
                ctx.stroke();
            }

            const nodeCount = profile.particles ? 9 : 6;
            for (let i = 0; i < nodeCount; i += 1) {
                const angle = (Math.PI * 2 * i / nodeCount) + (this.frame / 120);
                const radius = base + 56 + (i % 2) * 18;
                ctx.fillStyle = i % 3 === 0 ? palette.main : "rgba(219, 234, 254, .7)";
                ctx.beginPath();
                ctx.arc(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius, i % 3 === 0 ? 4 : 2.5, 0, Math.PI * 2);
                ctx.fill();
            }

            ctx.fillStyle = "#e0f2fe";
            ctx.font = "800 24px Inter, system-ui, sans-serif";
            ctx.textAlign = "center";
            ctx.fillText("Builder", cx, cy + 6);
            ctx.font = "600 12px Inter, system-ui, sans-serif";
            ctx.fillStyle = "#93c5fd";
            ctx.fillText(state.replace(/_/g, " ").toUpperCase(), cx, cy + 30);

            this.raf = window.requestAnimationFrame(this.draw);
        }
    }

    window.BusinessBuilderCore2D = BuilderCore2D;
})();
