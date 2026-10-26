import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Do not generate AGENTS.md / CLAUDE.md when `next dev` detects a coding agent.
  agentRules: false,
  // No dev-mode badge over the demo screens.
  devIndicators: false,
};

export default nextConfig;
