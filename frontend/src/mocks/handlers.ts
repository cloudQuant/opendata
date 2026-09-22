import { rest } from 'msw';

// Simple, yet functional mock handlers for development and tests
export const handlers = [
  // POST /api/auth/login
  rest.post('/api/auth/login', async (req, res, ctx) => {
    const { username } = req.body as { username?: string };
    return res(
      ctx.status(200),
      ctx.json({
        token: 'fake-jwt-token',
        user: { id: 'user-1', username: username ?? 'demo' },
      })
    );
  }),

  // POST /api/auth/register
  rest.post('/api/auth/register', async (req, res, ctx) => {
    const { username, email } = req.body as { username?: string; email?: string };
    return res(
      ctx.status(201),
      ctx.json({ id: 'new-user', username: username ?? 'new_user', email: email ?? 'example@example.com' })
    );
  }),

  // GET /api/scripts
  rest.get('/api/scripts', (req, res, ctx) => {
    // Minimal list to demonstrate behavior without heavy data
    return res(
      ctx.status(200),
      ctx.json([
        { id: 'script-1', name: 'Example Script 1' },
        { id: 'script-2', name: 'Example Script 2' }
      ])
    );
  }),
];

export default handlers;
