const outputElement = document.getElementById("output");

async function sendRequest(url, method = "GET", body = null) {
    try {
        const options = { method, headers: { "Content-Type": "application/json" } };
        if (body) options.body = JSON.stringify(body);

        const response = await fetch(url, options);
        const contentType = response.headers.get("content-type") || "";
        const result = contentType.includes("application/json")
            ? await response.json()
            : await response.text();

        if (!response.ok) {
            throw new Error(typeof result === "string" ? result : JSON.stringify(result));
        }

        outputElement.textContent = typeof result === "string"
            ? result
            : JSON.stringify(result, null, 2);
    } catch (error) {
        outputElement.textContent = `Error: ${error.message}`;
    }
}

document.body.addEventListener("click", (event) => {
    const action = event.target.dataset.action;
    if (!action) return;

    const passwordInput = document.getElementById("password");
    const password = passwordInput ? passwordInput.value.trim() : "";
    let url = "";
    let body = null;
    
    switch (action) {     
        ////////////////////////////  home.html  ////////////////////////////
        case "log-trace":
        case "log-debug":
        case "log-info":
        case "log-success":
        case "log-warning":
        case "log-error":
        case "log-critical":
            body = { password, log_level: action.replace("log-", "").toUpperCase() };
            url = "/log_level";
            break;
        case "reload-config":
            body = { password };
            url = "/config/reload";
            break;
        case "trigger-sync":
            const groupIdInput = document.getElementById("sync-group-id");
            const groupId = groupIdInput ? groupIdInput.value.trim() : "";
            body = { password, group_id: groupId || null };
            url = "/sync/trigger";
            break;
        case "view-config":
            body = { password };
            url = "/view_config";
            break;
        case "view-status":
            body = { password };
            url = "/view_status";
            break;
        case "portfolio-snapshot":
            body = { password };
            url = "/portfolio/snapshot";
            break;
        case "view-open-orders":
            body = { password };
            url = "/orders/open";
            break;
        case "on-whitelist":
            url = "/use_whitelist/1";
            break;
        case "off-whitelist":
            url = "/use_whitelist/0";
            break;
        case "pause-operation":
            body = { password };
            url = "/pause";
            break;
        case "resume-operation":
            body = { password };
            url = "/resume";
            break;

        ////////////////////////////  default  ////////////////////////////
        default:
            alert("Unknown action!");
            return;
    }

    sendRequest(url, body ? "POST" : "GET", body);
});
