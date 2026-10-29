"""Exercise the real HTTP/GraphQL adapter using a local fake Shopify server.

No live Shopify services or credentials are used. This checks transport and
workflow plumbing, not the correctness of Shopify's deployed API schema.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

import app
import migration
from fakes import nodes
from test_app import KEY, SECRET, SRC, DST, id_token


class HttpTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_http_transport_with_fake_shopify(self):
        image=(Path(__file__).resolve().parents[1]/'public'/'demo-product.png').read_bytes()
        seen={'queries':[],'uploads':[],'products':[],'metafields':[]}
        base={}
        upstream=web.Application(client_max_size=3*1024*1024)

        async def graphql(request):
            body=await request.json();query=body['query'];v=body.get('variables') or {}
            token=request.headers.get('X-Shopify-Access-Token')
            self.assertIn(token,('synthetic-source-token','synthetic-destination-token'))
            source=token=='synthetic-source-token';seen['queries'].append((source,query,v))
            if query==migration.QUERY_SHOP:
                data={'shop':{'id':'gid://shopify/Shop/101' if source else 'gid://shopify/Shop/202',
                              'name':'Fake source' if source else 'Fake destination',
                              'myshopifyDomain':SRC if source else DST,'currencyCode':'USD'},
                      'currentAppInstallation':{'accessScopes':[{'handle':s} for s in (
                          ['read_products','read_inventory'] if source else ['write_products','write_inventory','write_files'])]}}
            elif query==migration.MUTATION_BULK:
                self.assertTrue(source);self.assertEqual(v['query'],migration.BULK_PRODUCT_QUERY)
                data={'bulkOperationRunQuery':{'bulkOperation':{'id':'gid://shopify/BulkOperation/1','status':'CREATED'},'userErrors':[]}}
            elif query==migration.QUERY_BULK:
                self.assertTrue(source)
                data={'node':{'id':'gid://shopify/BulkOperation/1','status':'COMPLETED','objectCount':'5','url':base['url']+'/bulk'}}
            elif query==migration.QUERY_BY_HANDLE:
                self.assertFalse(source);data={'productByIdentifier':None}
            elif query==migration.MUTATION_STAGE:
                self.assertFalse(source)
                data={'stagedUploadsCreate':{'userErrors':[], 'stagedTargets':[
                    {'url':base['url']+'/upload','resourceUrl':'https://shopify.com/staged/fake.png',
                     'parameters':[{'name':'key','value':'fake-key'}]} for _ in v['input']]}}
            elif query==migration.MUTATION_SET_PRODUCT:
                self.assertFalse(source);doc=v['input'];seen['products'].append(doc)
                variants=[{'id':f'gid://shopify/ProductVariant/{i+800}',
                           'selectedOptions':[{'name':o['optionName'],'value':o['name']} for o in x['optionValues']]}
                          for i,x in enumerate(doc['variants'])]
                data={'productSet':{'product':{'id':'gid://shopify/Product/999','handle':doc['handle'],
                      'variants':{'nodes':variants},'media':{'nodes':[{'id':'gid://shopify/MediaImage/999',
                      'mediaContentType':'IMAGE','status':'READY'}]}},'userErrors':[]}}
            elif query==migration.MUTATION_SET_METAFIELDS:
                self.assertFalse(source);seen['metafields'].extend(v['metafields'])
                data={'metafieldsSet':{'metafields':[],'userErrors':[]}}
            else:
                raise AssertionError('Unexpected query')
            return web.json_response({'data':data})

        async def bulk(request):
            rows=list(nodes());rows[0]['descriptionHtml']='x'*180_000
            for row in rows:
                if row.get('image'):row['image']['url']=base['url']+'/image'
            return web.Response(body=b''.join((json.dumps(row)+'\n').encode() for row in rows),
                                content_type='application/x-ndjson')

        async def image_response(request):return web.Response(body=image,content_type='image/png')
        async def upload(request):
            reader=await request.multipart();parts={}
            while (part:=await reader.next()) is not None:parts[part.name]=await part.read()
            seen['uploads'].append(parts);return web.Response(status=204)
        upstream.router.add_post('/graphql',graphql);upstream.router.add_get('/bulk',bulk)
        upstream.router.add_get('/image',image_response);upstream.router.add_post('/upload',upload)
        mock=TestServer(upstream);await mock.start_server();base['url']=str(mock.make_url('/')).rstrip('/')

        class LocalClient(migration.ShopifyClient):
            def __init__(self,role):
                super().__init__(role);self.endpoint=base['url']+'/graphql'

        async def exchange(settings,token):
            claims=app.verify_id_token(token,settings)
            scopes='read_products,read_inventory' if settings.role=='source' else 'write_products,write_inventory,write_files'
            return {'access_token':'synthetic-'+settings.role+'-token','scope':scopes,'associated_user_scope':scopes,
                    'associated_user':{'id':int(claims['sub'])},'expires_in':3600}

        clients=[]
        try:
            with tempfile.TemporaryDirectory() as temp, patch.object(migration,'allowed_remote',lambda url,purpose:url.startswith(base['url'])):
                for role,shop in [('source',SRC),('destination',DST)]:
                    settings=app.Settings(role,KEY,SECRET,shop,Path(temp)/role,True)
                    server=app.create_app(settings,exchange=exchange,client_factory=LocalClient)
                    client=TestClient(TestServer(server));await client.start_server();clients.append((client,server))
                source,src_app=clients[0];dest,dest_app=clients[1]
                sh={'Authorization':'Bearer '+id_token(SRC)};dh={'Authorization':'Bearer '+id_token(DST)}
                response=await source.post('/api/migration/export',json={'mode':'shopify'},headers=sh)
                self.assertEqual(response.status,202);opid=(await response.json())['operation']['id']
                service=src_app[app.SERVICE_KEY];await asyncio.gather(*list(service.tasks.values()))
                row=service.operation(opid);self.assertEqual(row['status'],'completed',row['message'])
                bundle=Path(row['filepath']).read_bytes()
                form=FormData();form.add_field('file',bundle,filename='products.zip',content_type='application/zip')
                review=await dest.post('/api/migration/stage',data=form,headers=dh)
                self.assertEqual(review.status,200);stage=(await review.json())['stage']
                self.assertEqual(stage['counts']['product_metafields'],1)
                body={'stage_id':stage['stage_id'],'confirm_domain':DST,'acknowledge':True,'force_draft':True}
                response=await dest.post('/api/migration/import',json=body,headers=dh)
                self.assertEqual(response.status,202);import_id=(await response.json())['operation']['id']
                service=dest_app[app.SERVICE_KEY];await asyncio.gather(*list(service.tasks.values()))
                row=service.operation(import_id);self.assertEqual(row['status'],'completed',row['message'])
                self.assertEqual(len(seen['products']),1);self.assertEqual(seen['products'][0]['status'],'DRAFT')
                self.assertNotIn('id',seen['products'][0]);self.assertEqual(seen['uploads'][0]['file'],image)
                self.assertEqual(seen['metafields'][0]['key'],'material')
                source_queries=[q for is_source,q,_ in seen['queries'] if is_source]
                self.assertTrue(source_queries)
                self.assertTrue(set(source_queries).issubset({migration.QUERY_SHOP,migration.MUTATION_BULK,migration.QUERY_BULK}))
        finally:
            for client,_ in clients:await client.close()
            await mock.close()


if __name__=='__main__':unittest.main()
