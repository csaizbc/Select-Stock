export default async function handler(req, res) {
  const expectedAuth = (process.env.CRON_SECRET || "").trim();
  if (expectedAuth) {
    const authorization = req.headers.authorization || "";
    if (authorization !== `Bearer ${expectedAuth}`) {
      return res.status(401).json({ ok: false, error: "unauthorized" });
    }
  }

  const token = (process.env.GITHUB_DISPATCH_TOKEN || "").trim();
  const owner = (process.env.GITHUB_OWNER || "csaizbc").trim();
  const repo = (process.env.GITHUB_REPO || "Select-Stock").trim();
  const workflow = (process.env.GITHUB_WORKFLOW || "update.yml").trim();
  const ref = (process.env.GITHUB_REF || "main").trim();

  if (!token) {
    return res.status(500).json({ ok: false, error: "GITHUB_DISPATCH_TOKEN is missing" });
  }

  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "select-stock-vercel-cron",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref }),
    },
  );

  if (!response.ok) {
    const error = await response.text();
    return res.status(response.status).json({ ok: false, error });
  }

  return res.status(200).json({ ok: true, github_status: response.status });
}
