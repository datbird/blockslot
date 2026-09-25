"""cloudflare - a device's Cloudflare Access service token.

Optional. When the store is published through Cloudflare with an Access app
that admits service tokens, each
device needs its own token on top of its Garage key. With an API token, the
account id and the app id set in the web UI, adding a device makes the token
and adds it to the app's Service Auth policy; removing the device deletes it.

One token per device, like one key per device: a lost laptop is one revoke,
and the other devices keep working.
"""

import json
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
TOKEN_DURATION = "8760h"


class CloudflareError(Exception):
    """Cloudflare's API said no, or did not answer."""


def _default_opener(request, timeout=None):
    return urllib.request.urlopen(request, timeout=timeout)


class Cloudflare(object):
    def __init__(self, api_token, account_id, app_id, opener=None, timeout=20):
        self.api_token = api_token
        self.account_id = account_id
        self.app_id = app_id
        self.opener = opener or _default_opener
        self.timeout = timeout

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            API + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.api_token,
                     "Content-Type": "application/json",
                     "User-Agent": "BlockSlot-server/1"})
        try:
            raw = self.opener(request, timeout=self.timeout).read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
        except (urllib.error.URLError, OSError) as exc:
            raise CloudflareError("Cloudflare is not answering: %s" % getattr(exc, "reason", exc))
        try:
            answer = json.loads(raw or b"{}")
        except ValueError:
            raise CloudflareError("Cloudflare answered %s %s with something that is not JSON"
                                  % (method, path))
        if not answer.get("success"):
            messages = "; ".join(e.get("message", "") for e in answer.get("errors") or [])
            raise CloudflareError("Cloudflare refused %s %s: %s"
                                  % (method, path, messages or "no reason given"))
        return answer.get("result")

    # -- the app's Service Auth policy

    def _service_policy(self):
        """The policy on the app whose decision is Service Auth.

        An app made with inline policies has them converted to reusable ones
        by Cloudflare, so the app's own record lists them either way.
        """
        app = self.call("GET", "/accounts/%s/access/apps/%s" % (self.account_id, self.app_id))
        for policy in app.get("policies") or []:
            if policy.get("decision") == "non_identity":
                return policy
        raise CloudflareError("the Access app has no Service Auth policy to add devices to")

    def _put_policy(self, policy, include):
        body = {"name": policy.get("name"), "decision": "non_identity",
                "include": include, "exclude": policy.get("exclude") or [],
                "require": policy.get("require") or []}
        if policy.get("reusable") is False:
            path = "/accounts/%s/access/apps/%s/policies/%s" % (
                self.account_id, self.app_id, policy["id"])
            if policy.get("precedence") is not None:
                body["precedence"] = policy["precedence"]
        else:
            path = "/accounts/%s/access/policies/%s" % (self.account_id, policy["id"])
        self.call("PUT", path, body)

    # -- tokens

    def add_device(self, device):
        """Make blockslot-<device> and admit it. Returns (token id, client id, secret)."""
        made = self.call("POST", "/accounts/%s/access/service_tokens" % self.account_id,
                         {"name": "blockslot-%s" % device, "duration": TOKEN_DURATION})
        try:
            policy = self._service_policy()
            include = list(policy.get("include") or [])
            include.append({"service_token": {"token_id": made["id"]}})
            self._put_policy(policy, include)
        except CloudflareError:
            # A token no policy admits is useless; do not leave it behind.
            self.delete_token(made["id"])
            raise
        return made["id"], made["client_id"], made["client_secret"]

    def remove_device(self, token_id):
        policy = self._service_policy()
        include = [rule for rule in policy.get("include") or []
                   if (rule.get("service_token") or {}).get("token_id") != token_id]
        if len(include) != len(policy.get("include") or []):
            if not include:
                raise CloudflareError("removing this device would leave the Service Auth "
                                      "policy empty; Cloudflare refuses that. Remove it "
                                      "in the Cloudflare dashboard instead.")
            self._put_policy(policy, include)
        self.delete_token(token_id)

    def delete_token(self, token_id):
        self.call("DELETE", "/accounts/%s/access/service_tokens/%s" % (self.account_id, token_id))
