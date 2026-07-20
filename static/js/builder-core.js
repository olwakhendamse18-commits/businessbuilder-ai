(function () {
    "use strict";

    function initBuilderCore(stateController) {
        const canvas = document.getElementById("builderCoreCanvas");
        if (!canvas || !window.BusinessBuilderCore2D) return null;
        const core = new window.BusinessBuilderCore2D(canvas, stateController);
        core.start();
        stateController.addEventListener("preferenceschange", () => core.resize());
        return core;
    }

    window.BusinessBuilderCore = {
        init: initBuilderCore
    };
})();
