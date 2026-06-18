// SPDX-License-Identifier: Apache-2.0
// Passkey (WebAuthn) enrolment + login for the NEURON console.
// Talks to /passkeys/{register,login}/{options,verify}. The server (py_webauthn)
// emits/consumes base64url for all binary fields, so we convert to/from
// ArrayBuffers here.

(function () {
  "use strict";

  function b64urlToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = atob(s);
    var buf = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }

  function bufToB64url(buf) {
    var bytes = new Uint8Array(buf);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function showError(elId, message) {
    var el = elId && document.getElementById(elId);
    if (el) {
      el.textContent = message;
      el.hidden = false;
    } else {
      alert(message);
    }
  }

  async function postJson(url, body, headers) {
    var resp = await fetch(url, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
      body: body ? JSON.stringify(body) : "{}",
      credentials: "same-origin",
    });
    var data = {};
    try { data = await resp.json(); } catch (e) { /* options endpoints return JSON */ }
    if (!resp.ok) throw new Error((data && data.error) || ("Request failed (" + resp.status + ")"));
    return data;
  }

  function serializeCreate(cred) {
    var r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufToB64url(r.clientDataJSON),
        attestationObject: bufToB64url(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [],
      },
      clientExtensionResults: cred.getClientExtensionResults(),
    };
  }

  function serializeGet(cred) {
    var r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64url(cred.rawId),
      type: cred.type,
      response: {
        clientDataJSON: bufToB64url(r.clientDataJSON),
        authenticatorData: bufToB64url(r.authenticatorData),
        signature: bufToB64url(r.signature),
        userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
      },
      clientExtensionResults: cred.getClientExtensionResults(),
    };
  }

  window.neuronPasskeyRegister = async function (label, errorElId) {
    if (!window.PublicKeyCredential) {
      return showError(errorElId, "This browser does not support passkeys.");
    }
    try {
      var csrf = window.NEURON_CSRF || "";
      var opts = await postJson("/passkeys/register/options", {}, { "X-CSRF-Token": csrf });
      opts.challenge = b64urlToBuf(opts.challenge);
      opts.user.id = b64urlToBuf(opts.user.id);
      (opts.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
      var cred = await navigator.credentials.create({ publicKey: opts });
      await postJson(
        "/passkeys/register/verify",
        { credential: serializeCreate(cred), label: label },
        { "X-CSRF-Token": csrf }
      );
      window.location.reload();
    } catch (e) {
      showError(errorElId, e.message || String(e));
    }
  };

  window.neuronPasskeyLogin = async function (errorElId) {
    if (!window.PublicKeyCredential) {
      return showError(errorElId, "This browser does not support passkeys.");
    }
    try {
      var opts = await postJson("/passkeys/login/options", {});
      opts.challenge = b64urlToBuf(opts.challenge);
      (opts.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
      var cred = await navigator.credentials.get({ publicKey: opts });
      await postJson("/passkeys/login/verify", { credential: serializeGet(cred) });
      window.location.assign("/");
    } catch (e) {
      showError(errorElId, e.message || String(e));
    }
  };
})();
