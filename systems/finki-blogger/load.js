import http from 'k6/http';
import { check, sleep } from 'k6';
import { SharedArray } from 'k6/data';

const BASE_URL = __ENV.BLOG_BASE_URL || 'http://localhost:8000';
// Think-time between iterations, in seconds. Each VU used to pause 1-3s, which
// capped offered load at roughly VUs/2.2 iterations per second no matter how
// fast the system replied -- the machine sat ~80% idle while the model reported
// the services as saturated. THINK_TIME=0 (the default now) removes the cap and
// measures capacity; set THINK_TIME=2 to restore the human-paced workload.
const THINK_TIME = Number(__ENV.THINK_TIME ?? 0);
const LOGIN_EMAIL = 'test@example.com';
const LOGIN_PASSWORD = 'Test123!';

const blogTitles = new SharedArray('blog titles', function () {
  return [
    "Микросервиси во 2025",
    "Зошто k6 е подобар од JMeter",
    "Docker + Kubernetes = love",
    "Перформансно тестирање за магистри",
    "Моето прво вирално објавување"
  ];
});

const blogContents = new SharedArray('blog contents', function () {
  return [
    "Ова е тест блог генериран од k6 за стрес тестирање.",
    "Перформансите се клучни за секоја апликација.",
    "Микросервисите овозможуваат подобра скалабилност.",
    "Load testing помага да идентификуваме bottlenecks.",
    "Kubernetes е иднината на deployment."
  ];
});

export const options = {
  stages: [
    { duration: '30s', target: 100 },   // ramp-up to 100 VU
    { duration: '1m', target: 500 },    // ramp-up to 500 VU
    { duration: '1m30s', target: 1000 },// ramp-up to 1000 VU
    { duration: '1m', target: 2000 },   // peak at 2000 VU
    { duration: '1m', target: 0 },      // ramp-down
  ],
  thresholds: {
    http_req_duration: ['p(95)<800'],   
    http_req_failed: ['rate<0.01'],     
    'http_req_duration{name:GetBlogs}': ['p(95)<600'],
    'http_req_duration{name:CreateBlog}': ['p(95)<1500'],
    'http_req_duration{name:GetBlogDetails}': ['p(95)<700'],
    'http_req_duration{name:LikeBlog}': ['p(95)<600'],
    'http_req_duration{name:CreateComment}': ['p(95)<800'],
  },
};

export function setup() {
  const loginRes = http.post(
    `${BASE_URL}/user/Authentication/login`,
    JSON.stringify({
      email: LOGIN_EMAIL,
      password: LOGIN_PASSWORD,
    }),
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: '30s',
    }
  );

  const loginSuccess = check(loginRes, {
    'login successful (200)': (r) => r.status === 200,
    'login response is text': (r) => r.body && r.body.length > 0,
  });

  if (!loginSuccess) {
    console.error(`Login failed! Status: ${loginRes.status}, Body: ${loginRes.body}`);
    throw new Error('Login failed - check credentials and endpoint');
  }

  const token = loginRes.body;

  if (!token || token.length < 20) {
    console.error('Invalid token received!');
    throw new Error('Invalid token');
  }

  console.log('✓ Successful login! Token obtained.');
  return { token };
}

export default function (data) {
  const authHeaders = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Authorization': `Bearer ${data.token}`,
  };

  const blogsRes = http.get(`${BASE_URL}/blog/blogs?skip=0&limit=20`, {
    headers: authHeaders,
    tags: { name: 'GetBlogs' },
  });

  check(blogsRes, {
    'get blogs successful (200)': (r) => r.status === 200,
    'blogs response has items array': (r) => {
      try {
        const body = JSON.parse(r.body);
        return Array.isArray(body.items);
      } catch (e) {
        return false;
      }
    },
  });

  let existingBlogs = [];
  try {
    existingBlogs = JSON.parse(blogsRes.body).items;
  } catch (e) {
    console.warn('Could not parse blogs response');
  }

  let newBlogId = null;
  if (Math.random() < 0.2) {
    const title = blogTitles[Math.floor(Math.random() * blogTitles.length)];
    const content = blogContents[Math.floor(Math.random() * blogContents.length)];

    const createRes = http.post(
      `${BASE_URL}/blog/create`,
      JSON.stringify({
        title: title,
        blog_text: content + ` [k6 test @ ${new Date().toISOString()}]`,
      }),
      {
        headers: authHeaders,
        tags: { name: 'CreateBlog' },
      }
    );

    check(createRes, {
      'create blog successful': (r) => r.status === 200 || r.status === 201,
      'create blog returns ID': (r) => {
        try {
          const body = JSON.parse(r.body);
          return body.id || body.blog_id;
        } catch (e) {
          return false;
        }
      },
    });

    try {
      const blogData = JSON.parse(createRes.body);
      newBlogId = blogData.id || blogData.blog_id;
    } catch (e) {
      console.warn('Could not parse create blog response');
    }
  }

  if (Math.random() < 0.4 && existingBlogs.length > 0) {
    const randomBlog = existingBlogs[Math.floor(Math.random() * existingBlogs.length)];
    const blogId = randomBlog.id || randomBlog.blog_id;

    if (blogId) {
      const detailsRes = http.get(`${BASE_URL}/blog/blogs/${blogId}`, {
        headers: authHeaders,
        tags: { name: 'GetBlogDetails' },
      });

      check(detailsRes, {
        'get blog details successful': (r) => r.status === 200,
        'blog details has title': (r) => {
          try {
            const body = JSON.parse(r.body);
            return body.title && body.title.length > 0;
          } catch (e) {
            return false;
          }
        },
      });
    }
  }

  if (Math.random() < 0.6) {
    let blogIdToLike = newBlogId;

    if (!blogIdToLike && existingBlogs.length > 0) {
      const randomBlog = existingBlogs[Math.floor(Math.random() * existingBlogs.length)];
      blogIdToLike = randomBlog.id || randomBlog.blog_id;
    }

    if (blogIdToLike) {
      const likeRes = http.post(
        `${BASE_URL}/likes/like?blog_id=${blogIdToLike}`,
        '{}',
        {
          headers: authHeaders,
          tags: { name: 'LikeBlog' },
        }
      );

      check(likeRes, {
        'like blog successful': (r) => r.status === 200 || r.status === 201,
      });

      if (Math.random() < 0.5) {
        const hasLikedRes = http.get(
          `${BASE_URL}/likes/has-liked/${blogIdToLike}`,
          {
            headers: authHeaders,
            tags: { name: 'CheckLiked' },
          }
        );

        check(hasLikedRes, {
          'check liked successful': (r) => r.status === 200,
        });
      }
    }
  }

  if (Math.random() < 0.3 && existingBlogs.length > 0) {
    const randomBlog = existingBlogs[Math.floor(Math.random() * existingBlogs.length)];
    const blogId = randomBlog.id || randomBlog.blog_id;

    if (blogId) {
      const commentRes = http.post(
        `${BASE_URL}/comments/create-comment`,
        JSON.stringify({
          blog_id: blogId,
          comment_text: `Одличен блог! [k6 test comment @ ${Date.now()}]`,
        }),
        {
          headers: authHeaders,
          tags: { name: 'CreateComment' },
        }
      );

      check(commentRes, {
        'create comment successful': (r) => r.status === 200 || r.status === 201,
      });
    }
  }

  // jitter around THINK_TIME so VUs don't march in lockstep; skipped entirely
  // when THINK_TIME is 0, which is what lets the load find the real ceiling
  if (THINK_TIME > 0) {
    sleep(Math.random() * THINK_TIME + THINK_TIME * 0.5);
  }
}

export function teardown(data) {
  console.log('✓ Load test completed!');
  console.log('Check the k6 results above for performance metrics.');
}