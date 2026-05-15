/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — served by FastAPI at / under HA Ingress.
  // HA Ingress prefixes every request with a per-session path; the
  // exported bundle therefore must use only RELATIVE asset paths.
  // `assetPrefix: './'` makes _next/* references relative so the same
  // build works at any ingress prefix without a rebuild.
  output: 'export',
  assetPrefix: './',
  trailingSlash: true,
  reactStrictMode: true,
  images: {
    // Static export can't use Next image optimization.
    unoptimized: true,
  },
  eslint: {
    // `npm run lint` runs ESLint explicitly; don't double-run in build.
    ignoreDuringBuilds: true,
  },
  // No app/api routes — backend is FastAPI. A static export forbids
  // server route handlers; placing one under app/api/ breaks the build.
};

module.exports = nextConfig;
