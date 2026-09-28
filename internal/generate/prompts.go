package generate

import "strings"

import "github.com/synlace/vmfactory/internal/spec"

// buildPrompt assembles one lane's prompt, byte-identical to the
// reference's plan_method: head (the goal), the spec block when a
// spec exists, the per-method reply shape, the lane's measured
// paragraphs, grounding, and the evidence bundle.
func buildPrompt(m string, specJSON *map[string]any, grounded, bundle string) string {
	prompt := "Plan how to run this repository inside a disposable microVM " +
		"(the VM is the sandbox) using ONE install approach: " + Goal[m] + ".\n" +
		"Use ONLY the evidence below and the grounded facts; never " +
		"invent versions, ports, or env values. When this approach " +
		"cannot work for this repo, reply status=blocked with a " +
		"short why (max 8 words) instead of a guess.\n" +
		"Reply with ONE JSON object:\n"
	if sb := spec.Block(specJSON); sb != "" {
		prompt += sb
	}
	switch m {
	case "prebuilt":
		prompt += `{"status": "plan"|"blocked", "why": "<max 8 words>", ` +
			`"image": "<exact ref, e.g. docker.io/library/ghost:5>", ` +
			`"ports": [<guest tcp ports>], ` +
			`"install": ["<shell commands run once in the guest ` +
			`BEFORE the image starts>"], ` +
			`"env": {"K": "<literal value>"}, ` +
			`"checks": [{"probe": {"port": N, "path": "/", ` +
			`"expect_status_max": 399}} or an exec check that ` +
			`proves the app answers], ` +
			`"notes": "<max 8 words>"}` + "\n"
		prompt += "The image boots as one compose service; the " +
			"install list runs in the guest before it starts, " +
			"so it can prepare files or services the image " +
			"expects but does not carry. The boot is " +
			"unattended: nothing can click a button or answer " +
			"a wizard. Declare env the app requires from the " +
			"repo's .env.example or README — missing env " +
			"secrets kill apps after they bind. Env values " +
			"are literal: never put an explanation inside a " +
			"value; explanations belong in notes. A login " +
			"page is not success: if the app ships default " +
			"credentials, declare an exec check that logs in " +
			"and asserts post-auth content. If the image " +
			"cannot work without a sidecar this plan cannot " +
			"provide, reply status=blocked with a short why " +
			"— the verify fails a broken app honestly.\n"
	case "compose":
		prompt += `{"status": "plan"|"blocked", "why": "<max 8 words>", ` +
			`"compose_file": "<compose file in the repo ROOT>", ` +
			`"ports": [<guest tcp ports of the primary>], ` +
			`"install": ["<shell commands run once in the guest ` +
			`BEFORE compose up>"], ` +
			`"checks": [{"probe": {"port": N, "path": "/", ` +
			`"expect_status_max": 399}} or an exec check that ` +
			`proves the app answers], ` +
			`"notes": "<max 8 words>"}` + "\n"
		prompt += "The boot is unattended: nothing can click a " +
			"button or answer a wizard, so any one-time " +
			"initialization (database schema, migrations, a " +
			"setup page, a config file the stack expects) " +
			"must ride the install list as a deterministic " +
			"command run before compose up. A login page is " +
			"not success: if the app ships default " +
			"credentials, declare an exec check that logs in " +
			"and asserts post-auth content.\n"
	case "build":
		prompt += `{"status": "plan"|"blocked", "why": "<max 8 words>", ` +
			`"dockerfile": "Dockerfile", ` +
			`"ports": [<guest tcp ports>], ` +
			`"env": {"K": "<literal value>"}, ` +
			`"notes": "<max 8 words>"}` + "\n"
		prompt += "Declare env the app requires: missing env " +
			"secrets kill apps seconds after they bind " +
			"(paperclip measured BETTER_AUTH_SECRET). Env " +
			"values are literal: never put an explanation " +
			"inside a value; explanations belong in notes. " +
			"A login " +
			"page is not success: if the app ships default " +
			"credentials, say so in notes.\n"
	default: // pkg, source
		prompt += `{"status": "plan"|"blocked", "why": "<max 8 words>", ` +
			`"install": ["<shell commands run once>"], ` +
			`"command": ["<argv that starts the app>"], ` +
			`"ports": [<guest tcp ports>], ` +
			`"checks": [{"probe": {"port": N, "path": "/", ` +
			`"expect_status_max": 399}} or for a CLI tool ` +
			`{"cmd": {"bin": "<tool>", "probes": ` +
			`["<tool> --version", "<tool> --help"]}}, ` +
			`"user": "<run the app as this account>", ` +
			`"images": ["<container image the app needs>"], ` +
			`"env": {"K": "V"}, "needs_docker": <bool>, ` +
			`"memory_mb": <1024-8192>, "notes": "<max 8 words>"}` + "\n"
		if !isWeb(specJSON) {
			prompt += "If the app is a CLI tool (no server, no " +
				"ports), set command to [\"sleep\", " +
				"\"100000000\"] (a keep-alive so the VM " +
				"survives the verify), leave ports empty, " +
				"and declare the cmd check.\n"
		}
		prompt += "The boot is unattended: nothing can click a " +
			"button or answer a wizard, so any one-time " +
			"initialization (database schema, migrations, a " +
			"setup page) must ride the install list as a " +
			"deterministic command (a CLI runner, an SQL " +
			"import, or a scripted curl of the setup " +
			"endpoint). A login page is not success: if the " +
			"app ships default credentials, declare a check " +
			"that logs in and asserts post-auth content. If " +
			"the app refuses root (embedded postgres does), " +
			"create a dedicated account in the install list " +
			"(useradd -m app) and declare user.\n"
	}
	if grounded != "" {
		prompt += "Grounding:\n" + grounded + "\nEvidence:\n" + trunc32768(bundle)
	} else {
		prompt += "Evidence:\n" + trunc32768(bundle)
	}
	return prompt
}

func trunc32768(s string) string {
	if len(s) <= 32768 {
		return s
	}
	return s[:32768]
}

func isWeb(specJSON *map[string]any) bool {
	if specJSON == nil {
		return false
	}
	d := *specJSON
	deliv, _ := d["deliverable"].(string)
	return strings.EqualFold(deliv, "web")
}
