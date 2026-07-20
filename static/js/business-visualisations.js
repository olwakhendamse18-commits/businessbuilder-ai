(function () {
    "use strict";

    function updateProjectMap(nodes) {
        const root = document.getElementById("projectMap");
        if (!root || !Array.isArray(nodes)) return;
        root.querySelectorAll(".project-node").forEach((node) => {
            const match = nodes.find((item) => item.label === node.textContent.trim());
            if (match) node.dataset.status = String(match.status || "not started").toLowerCase().replace(/\s+/g, "-");
        });
    }

    function updateToolConstellation(tools) {
        const root = document.getElementById("toolConstellation");
        if (!root || !Array.isArray(tools)) return;
        root.replaceChildren();
        tools.forEach((tool) => {
            const item = document.createElement("span");
            item.dataset.state = tool.state || "unavailable";
            item.textContent = `${tool.label} · ${String(tool.state || "unavailable").replace(/_/g, " ")}`;
            root.appendChild(item);
        });
    }

    function updateDiagnostics(items) {
        const root = document.getElementById("diagnosticList");
        if (!root || !Array.isArray(items)) return;
        root.replaceChildren();
        items.forEach((item) => {
            const li = document.createElement("li");
            li.dataset.state = item.state || "could_not_verify";
            const label = document.createElement("span");
            label.textContent = item.label || "Diagnostic";
            const state = document.createElement("strong");
            state.textContent = String(item.state || "could_not_verify").replace(/_/g, " ");
            li.append(label, state);
            root.appendChild(li);
        });
    }

    function updateProductPipeline(stages) {
        const root = document.getElementById("productPipeline");
        if (!root || !Array.isArray(stages)) return;
        root.replaceChildren();
        stages.forEach((stage) => {
            const item = document.createElement("span");
            item.dataset.status = String(stage.status || "not started").toLowerCase().replace(/\s+/g, "-");
            item.textContent = `${stage.label || "Stage"} · ${stage.status || "Not started"}`;
            root.appendChild(item);
        });
    }

    function updateAlertRadar(alerts) {
        const root = document.getElementById("alertRadar");
        if (!root || !Array.isArray(alerts)) return;
        root.replaceChildren();
        if (!alerts.length) {
            const item = document.createElement("span");
            item.dataset.severity = "info";
            item.textContent = "No recent alerts";
            root.appendChild(item);
            return;
        }
        alerts.slice(0, 5).forEach((alert) => {
            const item = document.createElement("span");
            item.dataset.severity = alert.severity || "info";
            item.textContent = alert.title || "Alert";
            root.appendChild(item);
        });
    }

    function update(payload) {
        const source = payload || {};
        updateProjectMap(source.projectMap || source.project_map);
        updateToolConstellation(source.toolStatuses || source.tool_statuses);
        updateDiagnostics(source.diagnostics);
        updateProductPipeline(source.productPipeline || source.product_pipeline);
        updateAlertRadar(source.alertRadar || source.alert_radar);
    }

    window.BusinessBuilderVisualisations = {
        update,
        updateProjectMap,
        updateToolConstellation,
        updateDiagnostics,
        updateProductPipeline,
        updateAlertRadar
    };
})();
