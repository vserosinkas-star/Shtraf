import aiohttp
import asyncio

async def test():
    number = "С201УМ196"
    sts = "9955233535"  # ← замените!
    url = f"https://shtrafy-gibdd.ru/api/v1/fines?number={number}&sts={sts}"
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as resp:
            print("Status:", resp.status)
            print("Response:", await resp.text())

asyncio.run(test())