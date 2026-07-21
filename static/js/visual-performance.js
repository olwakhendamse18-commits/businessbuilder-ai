(function () {
    "use strict";

    function prefersReducedMotion() {
        return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    }

    function getProfile(preferences) {
        const prefs = preferences || {};
        const smallScreen = window.innerWidth < 760;
        const reduced = prefersReducedMotion() || prefs.motion_level === "reduced";
        const noMotion = prefs.motion_level === "none" || prefs.visual_mode === "minimal";
        const lowQuality = prefs.visual_quality === "low" || smallScreen || noMotion;
        return {
            reducedMotion: reduced,
            noMotion,
            lowQuality,
            pixelRatio: Math.min(window.devicePixelRatio || 1, lowQuality ? 1.25 : 1.75),
            particles: Boolean(prefs.show_particles) && !noMotion && !lowQuality,
            webglAllowed: Boolean(prefs.show_3d) && !noMotion && !smallScreen
        };
    }

    window.BusinessBuilderVisualPerformance = {
        prefersReducedMotion,
        getProfile
    };
})();
