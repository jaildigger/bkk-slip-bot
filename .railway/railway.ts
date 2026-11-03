import { defineRailway, github, project, service, volume } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "bkk-slip-bot";
export default defineRailway(() => {
  const bkkSlipBot = service("bkk-slip-bot", {
    source: github("jaildigger/bkk-slip-bot", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 120,
    // Ровно одна реплика: два экземпляра с одним токеном мешают друг другу, а SQLite — один файл.
    replicas: { "sfo": 1 },
    // База лежит здесь (RAILWAY_VOLUME_MOUNT_PATH → /data/payments.db). Без этой строки
    // apply отключил бы том, и бот стартовал бы с пустой базой.
    volumeMounts: {
      "/data": volume("bkk-slip-bot-volume", { region: "sfo", sizeMB: 5000 }),
    },
  });

  return project("proud-optimism", {
    variables: { managed: false },
    resources: [bkkSlipBot],
  });
});
