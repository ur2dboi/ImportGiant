"""Offline integration/security tests. All Shopify behavior is simulated."""
import asyncio
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import jwt
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import app
import migration
from fakes import FakeShopify

KEY='testing-client-id-12345'
SECRET='testing-only-secret-long-enough-12345'
SRC='source-test.myshopify.com'
DST='destination-test.myshopify.com'


def id_token(shop, *, key=KEY, secret=SECRET, delta=0, sub='555', sid='test-session'):
    now=int(time.time())
    return jwt.encode({'iss':f'https://{shop}/admin','dest':f'https://{shop}','aud':key,'sub':sub,'sid':sid,
                       'exp':now+60+delta,'nbf':now-5,'iat':now-5},secret,algorithm='HS256')


async def fake_exchange(settings, token):
    claims=app.verify_id_token(token,settings)
    scopes='read_products,read_inventory' if settings.role=='source' else 'write_products,write_inventory,write_files'
    return {'access_token':'not-a-real-token','scope':scopes,'associated_user_scope':scopes,
            'associated_user':{'id':int(claims['sub'])},'expires_in':86399}


class ImportGiantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        FakeShopify.created=[]; FakeShopify.written=[]
        self.clients=[]

    async def asyncTearDown(self):
        for client in self.clients: await client.close()
        self.temp.cleanup()

    async def client(self,role,shop, name, factory=FakeShopify):
        settings=app.Settings(role,KEY,SECRET,shop,self.root/name,True)
        server=app.create_app(settings,exchange=fake_exchange,client_factory=factory)
        client=TestClient(TestServer(server));await client.start_server();self.clients.append(client)
        return client,server

    def auth(self,shop): return {'Authorization':'Bearer '+id_token(shop)}

    async def wait_jobs(self,server):
        service=server[app.SERVICE_KEY]
        while service.tasks:
            await asyncio.gather(*list(service.tasks.values()))

    async def exported_bytes(self):
        source,server=await self.client('source',SRC,'source')
        response=await source.post('/api/migration/export',json={'mode':'shopify'},headers=self.auth(SRC))
        self.assertEqual(response.status,202)
        opid=(await response.json())['operation']['id']
        await self.wait_jobs(server)
        row=server[app.SERVICE_KEY].operation(opid)
        self.assertEqual(row['status'],'completed',row['message'])
        return Path(row['filepath']).read_bytes()

    async def stage_bytes(self,client,shop,package):
        form=FormData();form.add_field('file',package,filename='products.zip',content_type='application/zip')
        response=await client.post('/api/migration/stage',data=form,headers=self.auth(shop))
        self.assertEqual(response.status,200)
        return (await response.json())['stage']

    async def test_signed_shopify_session_is_required_and_restricted(self):
        client,server=await self.client('source',SRC,'source')
        denied=await client.get('/api/context')
        self.assertEqual(denied.status,401)
        self.assertEqual(denied.headers.get('X-Shopify-Retry-Invalid-Session-Request'),'1')
        self.assertEqual((await client.get('/apps/importgiant?shop=test')).status,200)
        wrong={'Authorization':'Bearer '+id_token(DST)}
        self.assertEqual((await client.get('/api/context',headers=wrong)).status,401)
        wrong={'Authorization':'Bearer '+id_token(SRC,secret='wrong-secret-long-enough-1234567890')}
        self.assertEqual((await client.get('/api/context',headers=wrong)).status,401)
        wrong={'Authorization':'Bearer '+id_token(SRC,key='wrong-client')}
        self.assertEqual((await client.get('/api/context',headers=wrong)).status,401)
        wrong={'Authorization':'Bearer '+id_token(SRC,delta=-120)}
        self.assertEqual((await client.get('/api/context',headers=wrong)).status,401)
        response=await client.get('/api/context',headers=self.auth(SRC))
        self.assertEqual(response.status,200)
        body=await response.json();self.assertEqual(body['shop'],SRC);self.assertTrue(body['read_only'])
        self.assertNotIn('not-a-real-token',json.dumps(body))
        self.assertNotIn(SECRET,repr(server[app.CONFIG_KEY]))
        self.assertEqual((await client.get('/healthz')).status,200)

    async def test_source_rejects_write_scopes_and_graphql_writes(self):
        # Legacy environment tokens must never bypass the Shopify user session.
        with patch.dict(os.environ, {'SHOPIFY_SOURCE_DOMAIN':SRC, 'SHOPIFY_SOURCE_ADMIN_TOKEN':'fake-secret'}):
            self.assertFalse(migration.get_config('source')['configured'])
        settings=app.Settings('source',KEY,SECRET,SRC,self.root/'x',True)
        claims=app.verify_id_token(id_token(SRC),settings)
        data={'access_token':'not-real','scope':'read_products,read_inventory,write_products',
              'associated_user_scope':'read_products,read_inventory,write_products','associated_user':{'id':555},'expires_in':3600}
        with self.assertRaisesRegex(ValueError,'non-read permissions'):
            app.validate_exchange(data,claims,settings)
        session={'role':'source','shop':SRC,'token':'not-a-real-token'}
        handle=migration.CURRENT_SHOP.set(session)
        try:
            source=migration.ShopifyClient('source')
            with self.assertRaisesRegex(ValueError,'read-only'):
                await source.graphql(migration.MUTATION_SET_PRODUCT,{'input':{}},mutation=False)
            with self.assertRaisesRegex(ValueError,'read-only'):
                await source.graphql(migration.MUTATION_BULK,{'query':'mutation { productDelete }'},mutation=True)
            self.assertFalse(migration.get_config('destination')['configured'])
        finally:migration.CURRENT_SHOP.reset(handle)

    async def test_scopes_and_user_identity_are_enforced(self):
        settings=app.Settings('destination',KEY,SECRET,DST,self.root/'x',True)
        claims=app.verify_id_token(id_token(DST),settings)
        base={'access_token':'not-real','scope':'write_products,write_inventory,write_files',
              'associated_user_scope':'write_products,write_inventory,write_files','associated_user':{'id':555},'expires_in':3600}
        self.assertIn('read_products',app.validate_exchange(base,claims,settings)['app_scopes'])
        for delta in ({'associated_user_scope':'read_products'}, {'scope':'write_products'}, {'associated_user':{'id':123}}, {'expires_in':1}):
            with self.assertRaises(ValueError): app.validate_exchange({**base,**delta},claims,settings)

    async def test_mock_export_and_confirmed_draft_import(self):
        source,source_app=await self.client('source',SRC,'source')
        dest,dest_app=await self.client('destination',DST,'destination')
        sh=self.auth(SRC);dh=self.auth(DST)
        self.assertEqual((await source.post('/api/migration/import',json={},headers=sh)).status,403)
        self.assertEqual((await source.post('/api/migration/export',json={'mode':'demo'},headers=sh)).status,400)
        r=await source.post('/api/migration/export',json={'mode':'shopify'},headers=sh)
        self.assertEqual(r.status,202);export_id=(await r.json())['operation']['id']
        await self.wait_jobs(source_app)
        source_ops=await (await source.get('/api/migration/operations',headers=sh)).json()
        self.assertEqual(source_ops['operations'][0]['status'],'completed')
        preview=await source.post('/api/migration/stage',json={'operation_id':export_id},headers=sh)
        self.assertEqual(preview.status,200)
        counts=(await preview.json())['stage']['counts']
        self.assertEqual((counts['products'],counts['variants'],counts['images'],counts['product_metafields']),(1,1,1,1))
        r=await source.post('/api/download-ticket',json={'operation_id':export_id,'kind':'package'},headers=sh)
        self.assertEqual(r.status,200);ticket=(await r.json())['ticket']
        download=await source.post('/download',data={'ticket':ticket})
        self.assertEqual(download.status,200);package=await download.read()
        self.assertTrue(package.startswith(b'PK'))
        self.assertEqual((await source.post('/download',data={'ticket':ticket})).status,404)
        form=FormData();form.add_field('file',package,filename='products.zip',content_type='application/zip')
        staged=await dest.post('/api/migration/stage',data=form,headers=dh)
        self.assertEqual(staged.status,200);stage=(await staged.json())['stage']
        base={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
        self.assertEqual((await dest.post('/api/migration/import',json={**base,'force_draft':False},headers=dh)).status,400)
        self.assertEqual((await dest.post('/api/migration/import',json={**base,'confirm_domain':'wrong.myshopify.com'},headers=dh)).status,400)
        self.assertEqual((await dest.post('/api/migration/import',json={**base,'acknowledge':False},headers=dh)).status,400)
        self.assertEqual(FakeShopify.created,[])
        result=await dest.post('/api/migration/import',json=base,headers=dh)
        self.assertEqual(result.status,202);import_id=(await result.json())['operation']['id']
        await self.wait_jobs(dest_app)
        ops=await (await dest.get('/api/migration/operations',headers=dh)).json()
        self.assertEqual(ops['operations'][0]['status'],'completed')
        self.assertEqual(len(FakeShopify.created),1);self.assertEqual(FakeShopify.created[0]['status'],'DRAFT')
        self.assertEqual(FakeShopify.written[0]['key'],'material')
        r=await dest.post('/api/download-ticket',json={'operation_id':import_id,'kind':'report'},headers=dh)
        self.assertEqual(r.status,200)
        report=await dest.post('/download',data={'ticket':(await r.json())['ticket']})
        self.assertEqual(report.status,200);self.assertIn(b'summary',await report.read())

    async def test_volume_binding_prevents_cross_shop_reuse(self):
        first=app.Settings('source',KEY,SECRET,SRC,self.root/'shared',True)
        app.create_app(first,exchange=fake_exchange,client_factory=FakeShopify)
        with self.assertRaisesRegex(ValueError,'another shop or role'):
            app.create_app(app.Settings('destination',KEY,SECRET,DST,self.root/'shared',True),exchange=fake_exchange,client_factory=FakeShopify)

    async def test_download_ticket_form_is_bounded_and_not_in_url(self):
        client,server=await self.client('source',SRC,'source')
        self.assertEqual((await client.post('/download',data={'ticket':'a'*5000})).status,413)
        self.assertEqual((await client.post('/download',json={'ticket':'bad'})).status,400)
        self.assertEqual((await client.get('/download?ticket=bad')).status,405)

    async def test_role_routes_and_json_body_limits(self):
        source,_=await self.client('source',SRC,'source')
        dest,_=await self.client('destination',DST,'destination')
        sh=self.auth(SRC);dh=self.auth(DST)
        self.assertEqual((await source.post('/api/migration/test',json={'role':'destination'},headers=sh)).status,403)
        self.assertEqual((await dest.post('/api/migration/test',json={'role':'source'},headers=dh)).status,403)
        self.assertEqual((await dest.post('/api/migration/export',json={'mode':'shopify'},headers=dh)).status,403)
        form=FormData();form.add_field('file',b'nope',filename='x.zip')
        self.assertEqual((await source.post('/api/migration/stage',data=form,headers=sh)).status,403)
        self.assertEqual((await dest.post('/api/migration/stage',json={'operation_id':'x'},headers=dh)).status,400)
        too_large=json.dumps({'mode':'shopify','padding':'a'*(70*1024)})
        self.assertEqual((await source.post('/api/migration/export',data=too_large,
            headers={**sh,'Content-Type':'application/json'})).status,413)

    async def test_package_tampering_and_unsafe_paths_are_rejected(self):
        package=await self.exported_bytes()
        with zipfile.ZipFile(io.BytesIO(package)) as source:
            asset=next(name for name in source.namelist() if name.startswith('images/'))
            for variant in ('tampered','traversal'):
                dest=self.root/(variant+'.zip')
                with zipfile.ZipFile(dest,'w') as output:
                    for name in source.namelist():
                        raw=source.read(name)
                        if variant=='tampered' and name==asset:raw=bytes([raw[0]^1])+raw[1:]
                        output.writestr(name,raw)
                    if variant=='traversal':output.writestr('../escape',b'bad')
                with self.assertRaisesRegex(ValueError,'checksum|unsafe'):
                    migration.package_preflight(dest)

    async def test_same_shop_currency_and_demo_package_are_blocked(self):
        package=await self.exported_bytes()
        same,_=await self.client('destination',SRC,'same')
        stage=await self.stage_bytes(same,SRC,package)
        body={'stage_id':stage['stage_id'],'confirm_domain':SRC,'acknowledge':True,'force_draft':True}
        response=await same.post('/api/migration/import',json=body,headers=self.auth(SRC))
        self.assertEqual(response.status,400);self.assertIn('same',str(await response.json()).lower())

        class EuroShop(FakeShopify):
            async def shop(self):
                data=await super().shop();data['currencyCode']='EUR';return data
        dest,_=await self.client('destination',DST,'euro',EuroShop)
        stage=await self.stage_bytes(dest,DST,package)
        body={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
        response=await dest.post('/api/migration/import',json=body,headers=self.auth(DST))
        self.assertEqual(response.status,400);self.assertIn('currenc',str(await response.json()).lower())

        result=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(package)) as source,zipfile.ZipFile(result,'w') as output:
            for name in source.namelist():
                raw=source.read(name)
                if name=='manifest.json':
                    manifest=json.loads(raw);manifest['source']['kind']='demo';raw=json.dumps(manifest).encode()
                output.writestr(name,raw)
        realdest,real_app=await self.client('destination',DST,'demo-dest')
        stage=await self.stage_bytes(realdest,DST,result.getvalue())
        body={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
        response=await realdest.post('/api/migration/import',json=body,headers=self.auth(DST))
        self.assertEqual(response.status,400);self.assertIn('demo package',str(await response.json()).lower())
        # If a reviewed file is replaced on disk, import must recheck its source.
        stage=await self.stage_bytes(realdest,DST,package)
        with real_app[app.SERVICE_KEY].db() as conn:
            stored=conn.execute('SELECT filepath FROM migration_stages WHERE id=?',(stage['stage_id'],)).fetchone()
        Path(stored['filepath']).write_bytes(result.getvalue())
        body['stage_id']=stage['stage_id']
        response=await realdest.post('/api/migration/import',json=body,headers=self.auth(DST))
        self.assertEqual(response.status,400);self.assertIn('demo package',str(await response.json()).lower())
        self.assertEqual(FakeShopify.created,[])

    async def test_existing_products_are_not_modified(self):
        package=await self.exported_bytes()
        class ExistingShop(FakeShopify):
            async def find_product(self,handle):return {'id':'gid://shopify/Product/998','handle':handle}
        dest,server=await self.client('destination',DST,'existing',ExistingShop)
        stage=await self.stage_bytes(dest,DST,package)
        body={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
        response=await dest.post('/api/migration/import',json=body,headers=self.auth(DST))
        self.assertEqual(response.status,202);opid=(await response.json())['operation']['id']
        await self.wait_jobs(server);row=server[app.SERVICE_KEY].operation(opid)
        self.assertEqual(row['status'],'completed');self.assertEqual(json.loads(row['stats'])['skipped_existing'],1)
        self.assertEqual(FakeShopify.created,[]);self.assertEqual(FakeShopify.written,[])
        self.assertIn('skipped_existing',Path(row['reportpath']).read_text())

    async def test_ambiguous_write_stops_instead_of_retrying(self):
        package=await self.exported_bytes()
        class UnknownShop(FakeShopify):
            calls=0
            async def create_product(self,doc):
                type(self).calls+=1
                raise RuntimeError('The Shopify write outcome is unknown. Check the destination.')
        dest,server=await self.client('destination',DST,'unknown',UnknownShop)
        stage=await self.stage_bytes(dest,DST,package)
        body={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
        response=await dest.post('/api/migration/import',json=body,headers=self.auth(DST))
        self.assertEqual(response.status,202);opid=(await response.json())['operation']['id']
        await self.wait_jobs(server);row=server[app.SERVICE_KEY].operation(opid)
        self.assertEqual(row['status'],'failed');self.assertEqual(UnknownShop.calls,1)
        self.assertIn('fatal',Path(row['reportpath']).read_text())
        self.assertFalse(server[app.SERVICE_KEY].running_live_import)

    async def test_stale_exchange_asks_app_bridge_to_retry(self):
        async def stale_exchange(settings,token):
            raise app.RetryShopifySession('A fresh session is needed.')
        settings=app.Settings('source',KEY,SECRET,SRC,self.root/'stale',True)
        server=app.create_app(settings,exchange=stale_exchange,client_factory=FakeShopify)
        client=TestClient(TestServer(server));await client.start_server();self.clients.append(client)
        response=await client.get('/api/context',headers=self.auth(SRC))
        self.assertEqual(response.status,401)
        self.assertEqual(response.headers.get('X-Shopify-Retry-Invalid-Session-Request'),'1')

    async def test_import_and_download_require_fresh_user_scopes(self):
        can_write=True
        async def exchange(settings,token):
            data=await fake_exchange(settings,token)
            if not can_write:data['associated_user_scope']='read_products'
            return data
        settings=app.Settings('destination',KEY,SECRET,DST,self.root/'fresh',True)
        server=app.create_app(settings,exchange=exchange,client_factory=FakeShopify)
        client=TestClient(TestServer(server));await client.start_server();self.clients.append(client)
        headers=self.auth(DST)
        self.assertEqual((await client.get('/api/context',headers=headers)).status,200)
        can_write=False
        # These routes must not reuse the earlier cached token/scope check.
        self.assertEqual((await client.post('/api/migration/import',json={},headers=headers)).status,403)
        self.assertEqual((await client.post('/api/download-ticket',json={},headers=headers)).status,403)
        self.assertEqual(FakeShopify.created,[])


if __name__=='__main__': unittest.main()
