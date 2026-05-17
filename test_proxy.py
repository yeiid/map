import asyncio
import httpx

async def main():
    async with httpx.AsyncClient(base_url='http://localhost:3000') as client:
        # Martin catalog
        res = await client.get('/')
        print("Martin / status:", res.status_code)
        
        # Some tile
        res2 = await client.get('/dxf_u_construccion/0/0/0')
        print("Martin tile status:", res2.status_code)
        print("Martin tile headers:", res2.headers)
        print("Content length:", len(res2.content))

asyncio.run(main())
