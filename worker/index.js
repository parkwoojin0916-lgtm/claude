// Cloudflare Worker: 모든 요청을 컨테이너 안의 FastAPI로 넘긴다.
import { Container, getContainer } from "@cloudflare/containers";

export class ShortsCutter extends Container {
  // Dockerfile의 uvicorn 포트
  defaultPort = 8000;
  // 영상 처리가 길어서 넉넉히. 유휴 시 잠들어 과금이 멈춘다.
  sleepAfter = "20m";

  envVars = {
    DATA_DIR: "/data",
    MAX_WORKERS: "1",
  };

  onError(error) {
    return new Response(
      JSON.stringify({ detail: "컨테이너 오류: " + String(error) }),
      { status: 500, headers: { "content-type": "application/json" } },
    );
  }
}

export default {
  async fetch(request, env) {
    // 작업 상태가 컨테이너 메모리에 있으므로 모든 요청을 같은 인스턴스로 보낸다.
    return getContainer(env.SHORTS_CUTTER, "main").fetch(request);
  },
};
