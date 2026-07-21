(function () {
    "use strict";

    class BusinessBuilderWaveform {
        constructor(canvas) {
            this.canvas = canvas;
            this.ctx = canvas ? canvas.getContext("2d") : null;
            this.audioContext = null;
            this.analyser = null;
            this.source = null;
            this.data = null;
            this.animationId = null;
            this.mode = "idle";
            this.reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        }

        attachStream(stream) {
            this.detach();
            if (!this.canvas || !stream || this.reducedMotion) {
                this.mode = stream ? "listening" : "idle";
                this.drawFallback();
                return;
            }
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) {
                this.drawFallback();
                return;
            }
            this.audioContext = new AudioContext();
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            this.source = this.audioContext.createMediaStreamSource(stream);
            this.source.connect(this.analyser);
            this.data = new Uint8Array(this.analyser.frequencyBinCount);
            this.start("listening");
        }

        start(mode) {
            this.mode = mode || "listening";
            this.stopAnimation();
            const tick = () => {
                this.draw();
                this.animationId = window.requestAnimationFrame(tick);
            };
            tick();
        }

        setMode(mode) {
            this.mode = mode || "idle";
            if (!this.animationId) {
                this.start(this.mode);
            }
        }

        draw() {
            if (!this.ctx || !this.canvas) return;
            const width = this.canvas.width;
            const height = this.canvas.height;
            this.ctx.clearRect(0, 0, width, height);
            this.ctx.fillStyle = "rgba(2, 6, 23, 0.3)";
            this.ctx.fillRect(0, 0, width, height);

            const bars = 48;
            const gap = 4;
            const barWidth = Math.max(4, (width - gap * bars) / bars);
            let values = [];

            if (this.analyser && this.data) {
                this.analyser.getByteFrequencyData(this.data);
                for (let i = 0; i < bars; i += 1) {
                    values.push(this.data[Math.floor(i * this.data.length / bars)] / 255);
                }
            } else {
                const now = Date.now() / 260;
                for (let i = 0; i < bars; i += 1) {
                    values.push(0.16 + Math.abs(Math.sin(now + i * 0.38)) * (this.mode === "speaking" ? 0.58 : 0.22));
                }
            }

            const gradient = this.ctx.createLinearGradient(0, 0, width, 0);
            gradient.addColorStop(0, this.mode === "error" ? "#fb7185" : "#14b8a6");
            gradient.addColorStop(0.72, this.mode === "approval" ? "#fb923c" : "#60a5fa");
            gradient.addColorStop(1, "#a78bfa");
            this.ctx.fillStyle = gradient;

            values.forEach((value, index) => {
                const barHeight = Math.max(5, value * (height - 18));
                const x = index * (barWidth + gap) + gap;
                const y = (height - barHeight) / 2;
                this.roundRect(x, y, barWidth, barHeight, 10);
                this.ctx.fill();
            });
        }

        drawFallback() {
            if (!this.ctx || !this.canvas) return;
            this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
            this.ctx.fillStyle = "rgba(20, 184, 166, .28)";
            this.roundRect(18, this.canvas.height / 2 - 5, this.canvas.width - 36, 10, 999);
            this.ctx.fill();
        }

        roundRect(x, y, width, height, radius) {
            const r = Math.min(radius, width / 2, height / 2);
            this.ctx.beginPath();
            this.ctx.moveTo(x + r, y);
            this.ctx.arcTo(x + width, y, x + width, y + height, r);
            this.ctx.arcTo(x + width, y + height, x, y + height, r);
            this.ctx.arcTo(x, y + height, x, y, r);
            this.ctx.arcTo(x, y, x + width, y, r);
            this.ctx.closePath();
        }

        stopAnimation() {
            if (this.animationId) {
                window.cancelAnimationFrame(this.animationId);
                this.animationId = null;
            }
        }

        detach() {
            this.stopAnimation();
            try {
                if (this.source) this.source.disconnect();
            } catch (error) {}
            try {
                if (this.audioContext) this.audioContext.close();
            } catch (error) {}
            this.source = null;
            this.analyser = null;
            this.audioContext = null;
            this.data = null;
            this.mode = "idle";
        }

        stop() {
            this.detach();
            this.drawFallback();
        }
    }

    window.BusinessBuilderWaveform = BusinessBuilderWaveform;
})();
