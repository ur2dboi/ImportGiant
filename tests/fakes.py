"""Synthetic Shopify adapter; never contacts a real shop."""
import hashlib
from pathlib import Path
import migration

SRC = 'gid://shopify/Shop/101'
DST = 'gid://shopify/Shop/202'
PRODUCT = 'gid://shopify/Product/1'
VARIANT = 'gid://shopify/ProductVariant/11'
IMAGE = 'gid://shopify/MediaImage/21'


def nodes():
    yield {'__typename':'Product','id':PRODUCT,'handle':'test-mug','title':'Test Mug','status':'ACTIVE','tags':['demo'],
           'options':[{'name':'Title','position':1,'values':['Default Title']}], 'seo':{'title':'Mug','description':'Demo'}}
    yield {'__typename':'ProductVariant','id':VARIANT,'__parentId':PRODUCT,'sku':'MUG-1','price':'12.00',
           'selectedOptions':[{'name':'Title','value':'Default Title'}], 'inventoryItem':{'sku':'MUG-1','tracked':True}}
    yield {'__typename':'MediaImage','id':IMAGE,'__parentId':VARIANT}
    yield {'__typename':'MediaImage','id':IMAGE,'__parentId':PRODUCT,'alt':'Demo image','mediaContentType':'IMAGE',
           'status':'READY','mimeType':'image/png','image':{'url':'https://cdn.shopify.com/test.png'}}
    yield {'__typename':'Metafield','id':'gid://shopify/Metafield/31','__parentId':PRODUCT,
           'namespace':'custom','key':'material','type':'single_line_text_field','value':'Ceramic'}


class FakeShopify:
    created = []
    written = []

    def __init__(self, role):
        self.role=role
        self.config=migration.get_config(role)
        if not self.config['configured']:
            raise ValueError('No authenticated store for that role.')
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return None
    async def shop(self):
        source=self.role=='source'
        return {'id':SRC if source else DST,'myshopifyDomain':self.config['domain'],'name':'Source test' if source else 'Destination test',
                'currencyCode':'USD','scopes':['read_products','read_inventory'] if source else ['write_products','write_inventory','write_files']}
    async def bulk(self, query, progress):
        await progress('Mock bulk query',0,0)
        for node in nodes(): yield dict(node)
    async def download_image(self,url,destination,declared_type=None):
        raw=(Path(__file__).resolve().parents[1]/'public'/'demo-product.png').read_bytes()
        Path(destination).write_bytes(raw)
        return {'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'extension':'png','content_type':'image/png'}
    async def find_product(self,handle): return None
    async def stage_images(self,z,image_entries,assets):
        result={}
        for image in image_entries:
            if image.get('mediaContentType')=='IMAGE':
                path=image['asset'];assert hashlib.sha256(z.read(path)).hexdigest()==assets[path]['sha256']
                result[path]={'resourceUrl':'https://staged.invalid/image','filename':'image.png'}
        return result
    async def create_product(self,input_doc):
        type(self).created.append(input_doc)
        variants=[]
        for i,v in enumerate(input_doc['variants']):
            variants.append({'id':f'gid://shopify/ProductVariant/{900+i}',
                             'selectedOptions':[{'name':x['optionName'],'value':x['name']} for x in v.get('optionValues',[])]})
        return {'id':'gid://shopify/Product/999','handle':input_doc['handle'],'variants':{'nodes':variants},
                'media':{'nodes':[{'id':'gid://shopify/MediaImage/999','mediaContentType':'IMAGE','status':'READY'}]}}
    async def set_metafields(self,fields): type(self).written.extend(fields);return []
