import 'dotenv/config';
import express from 'express';
import { createServer } from 'node:http';
import { WebSocketServer } from 'ws';

const app = express();
const server = createServer(app);
const browserSockets = new Set();
const pythonServiceUrl = process.env.PYTHON_SERVICE_URL || 'http://127.0.0.1:8000';
const port = Number(process.env.NODE_PORT || 3000);

app.use(express.static(new URL('./public', import.meta.url).pathname));

app.get('/api/state', async (_request, response) => {
  try {
    const result = await fetch(`${pythonServiceUrl}/api/state`);
    response.status(result.status).json(await result.json());
  } catch (error) {
    response.status(503).json({ status: 'offline', error: error.message });
  }
});

app.get('/api/positions', async (_request, response) => {
  try {
    const result = await fetch(`${pythonServiceUrl}/api/positions`);
    response.status(result.status).json(await result.json());
  } catch (error) {
    response.status(503).json({ open: [], closed: [], summary: {}, error: error.message });
  }
});

app.post('/api/positions/:id/close', async (request, response) => {
  const id = Number(request.params.id);
  if (!Number.isInteger(id)) {
    response.status(400).json({ closed: false, error: 'invalid position id' });
    return;
  }
  try {
    const result = await fetch(`${pythonServiceUrl}/api/positions/${id}/close`, { method: 'POST' });
    response.status(result.status).json(await result.json());
  } catch (error) {
    response.status(503).json({ closed: false, error: error.message });
  }
});

const websocketServer = new WebSocketServer({ server, path: '/ws' });
websocketServer.on('connection', (socket) => {
  browserSockets.add(socket);
  socket.on('close', () => browserSockets.delete(socket));
});

setInterval(async () => {
  if (browserSockets.size === 0) return;
  try {
    const result = await fetch(`${pythonServiceUrl}/api/state`);
    const payload = await result.text();
    for (const socket of browserSockets) {
      if (socket.readyState === 1) socket.send(payload);
    }
  } catch {
    // The browser keeps its last known state while Python is unavailable.
  }
}, 1000);

server.listen(port, () => {
  console.log(`Dashboard listening at http://localhost:${port}`);
});
