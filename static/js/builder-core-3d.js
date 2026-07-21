(function () {
    "use strict";

    // Milestone 5 keeps the full interface usable without WebGL or third-party
    // runtime scripts. This adapter intentionally reports unavailable unless a
    // pinned local 3D renderer is added in a later milestone.
    window.BusinessBuilderCore3D = {
        available: false,
        init: function () {
            return null;
        }
    };
})();
