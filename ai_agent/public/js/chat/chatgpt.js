/*
 * "ChatGPT account": each person signs in with their own ChatGPT account, and an AI provider of type
 * ChatGPT Subscription answers them through their plan (ai_agent/chatgpt). OpenAI returns the browser to an
 * address on the person's own computer (127.0.0.1), which a server cannot receive: the person copies that
 * address from the browser and pastes it here. Nothing here holds a token: the server keeps them encrypted.
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const API = "ai_agent.chatgpt.api.";

    function status() {
        return frappe.xcall(API + "get_status");
    }

    function open(onChange) {
        const d = new frappe.ui.Dialog({ title: __("ChatGPT account"), size: "small" });
        const done = () => onChange && onChange();
        function show(st) {
            const body = $(d.body).empty();
            if (st.shared && !st.shared.is_me) {
                // one account for the whole site: nobody else signs in
                body.append($("<p>").text(st.shared.signed_in
                    ? __("This site uses one shared ChatGPT account for everyone. You do not need to sign in.")
                    : __("This site uses one shared ChatGPT account, which needs to sign in again. Ask your administrator.")));
                if (st.shared.email) body.append($("<p class='bold'>").attr("dir", "auto").text(st.shared.email));
                return;
            }
            if (st.signed_in && st.shared && st.shared.is_me) {
                body.append($("<p class='text-muted small'>").text(__("Everyone on this site uses this account (the provider's shared account).")));
            }
            if (st.signed_in) {
                body.append($("<p>").text(__("Your chats with the ChatGPT provider use your own ChatGPT plan through this account:")));
                body.append($("<p class='bold'>").attr("dir", "auto").text(st.email || "—"));
                body.append($("<p class='text-muted small'>").text(__("Usage counts against your ChatGPT plan. See it in ChatGPT settings, Usage.")));
                const out = $("<button class='btn btn-default btn-sm'>").text(__("Sign out"));
                const again = $("<button class='btn btn-default btn-sm ml-2'>").text(__("Sign in with another account"));
                out.on("click", () => frappe.xcall(API + "sign_out").then((r) => {
                    frappe.show_alert({ message: r.revoked ? __("Signed out of ChatGPT") : __("Signed out here; ChatGPT did not confirm it. You can disconnect the app in ChatGPT settings."), indicator: r.revoked ? "green" : "orange" });
                    done();
                    status().then(show);
                }));
                again.on("click", () => steps(st));
                body.append($("<div class='mt-3'>").append(out, again));
                return;
            }
            steps(st);
        }
        function steps(st) {
            const body = $(d.body).empty();
            if (st.status === "Sign in again") body.append($("<p class='text-warning'>").text(__("Your ChatGPT sign-in has ended. Sign in again.")));
            const go = $("<button class='btn btn-primary btn-sm'>").text(__("Continue with ChatGPT"));
            const address = $("<textarea class='form-control' rows='3' dir='ltr'>").attr("placeholder", "http://127.0.0.1:48155/auth/callback?code=…");
            const finish = $("<button class='btn btn-primary btn-sm mt-2'>").text(__("Complete sign-in")).prop("disabled", true);
            const note = $("<div class='text-muted small mt-2'>");
            body.append(
                $("<p>").text(__("1. Sign in with your ChatGPT account and allow it.")),
                go,
                $("<p class='mt-3'>").text(__("2. ChatGPT then opens a page that does not load (its address starts with http://127.0.0.1). Copy the whole address from the browser's address bar and paste it here:")),
                address,
                finish,
                note
            );
            const link = $("<a class='btn btn-default btn-sm ml-2' target='_blank' rel='noopener noreferrer'>").text(__("Open the sign-in page")).hide();
            go.after(link);
            go.on("click", () => {
                // opened now, inside the click: a window opened after waiting for the server is blocked by browsers
                const tab = window.open("about:blank", "_blank");
                frappe.xcall(API + "start_sign_in").then((r) => {
                    link.attr("href", r.url).show();   // also a plain link, should the browser have blocked the tab
                    if (tab && !tab.closed) {
                        tab.opener = null;
                        tab.location.href = r.url;
                        note.text(__("The sign-in page opened in a new tab. Finish there, then paste the address here."));
                    } else {
                        note.text(__("Your browser blocked the new tab: use «Open the sign-in page»."));
                    }
                    finish.prop("disabled", false);
                }).catch(() => tab && tab.close());
            });
            finish.on("click", () => {
                const value = (address.val() || "").trim();
                if (!value) return;
                finish.prop("disabled", true);
                frappe.xcall(API + "finish_sign_in", { address: value }).then((r) => {
                    frappe.show_alert({ message: __("Signed in to ChatGPT as {0}", [r.email || ""]), indicator: "green" });
                    done();
                    show(r);
                }).catch(() => finish.prop("disabled", false));
            });
        }
        status().then(show);
        d.show();
        return d;
    }

    AIChat.chatgpt = { status, open };
})();
