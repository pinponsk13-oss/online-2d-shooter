# ONLINE 2D SHOOTER - public prototype

- 1vs1オンライン
- 全員同一性能
- ブラウザ（iPad/スマホ/PC）でプレイ
- WebSocketリアルタイム同期
- 左半分移動 / 右半分エイム＋射撃
- 360度エイム＋エイム線
- HP、弾数、リロード、キル、リスポーン
- 広いマップ＋障害物
- 右上ミニマップ（自分と障害物のみ。敵は非表示）
- サーバー側でルームを正式作成
- `/?room=ABC123` の共有URLで直接参加

## Render
Runtime: Python

Build Command:
`pip install -r requirements.txt`

Start Command:
`uvicorn server:app --host 0.0.0.0 --port $PORT`

公開後、トップページで「ゲームを作る」を押すとルームIDが発行されます。
表示されたURLをLINEで共有すると、相手が同じルームへ参加できます。

※無料プランのクラウドサービスでは、一定時間アクセスがないとサービスが停止する場合があります。
