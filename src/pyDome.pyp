import math
import os
import pickle
import random
import re
import struct
import sys

import c4d
from c4d import bitmaps
from c4d import plugins
from c4d import storage
from c4d import utils

### TODO: add support for multipliers and gamma in env and refl images
### TODO: add support for header's "height" value
### TODO: add option to create physical sky if lat/lon is specified. also date/time
### TODO: possibly make the various skys virtual which would make it easier to manage, but we'd still have to manage materials.
### TODO: support low discrepancy sampling (hammersly, poisson, multi-jittered, maybe uniform)
### TODO: support for other sampling methods
### TODO: support for uv offsets
### TODO: confirm uv coordinates... since we're looking at a sky from the inside we might want to provide an option to mirror uvs

PYDOME_BUILD_DATE = '20150114'

# some PI constants
PI          = math.pi
PI2         = PI*2.0
PI05        = PI*0.5

# these regex patterns are used for parsing the ibl files
IBL_SECTION = re.compile('^\[(.*)\]$')
IBL_LIGHT   = re.compile('^light([\d]+)$')
IBL_VALUE   = re.compile('^(.*?) = (.*?)$')
IBL_COLOR   = re.compile('^([\d]{1,3}),([\d]{1,3}),([\d]{1,3})$')
IBL_STRING  = re.compile('^["](.*)["]$')
IBL_FLOAT   = re.compile('^([-]?[\d]+[.][\d]+)$')
IBL_INT     = re.compile('^([\d]+)$')

# material names
MATNAME_BKG = 'pyDome_MAT_BKG'
MATNAME_ENV = 'pyDome_MAT_ENV'
MATNAME_REF = 'pyDome_MAT_REF'

# object names
OBJNAME_BKG = 'pyDome_SKY_BKG'
OBJNAME_ENV = 'pyDome_SKY_ENV'
OBJNAME_REF = 'pyDome_SKY_REF'

PLUGIN_ID   = 1017232 # use your own plugin id from www.plugincafe.com

class PyDome(plugins.ObjectData):
    def __init__(self):
        self._last_path = os.path.expanduser('~') # last path that was browsed through LoadDialog()
        self._env_image = None # copy of the environment image, cached for speed
        self._ibl_dict = None # a dictionary representation of the contents of the loaded ibl file, or None

        self.SetOptimizeCache(True)

        return super(self.__class__, self).__init__()

    def getEnvColor(self, uv):
        """Calculates color from self._env_image
        Args:
            uv (tuple): texture coordinates to sample
        Returns:
            (vector): sampled color (un-normalized)
        """
        bytes = self._env_image.GetBt()/8
        xpos = int(uv[0]*float(self._env_image.GetBw()))
        ypos = int(uv[1]*float(self._env_image.GetBh()))
        buffer = c4d.storage.ByteSeq(None, bytes)
        try:
            self._env_image.GetPixelCnt(xpos, ypos, 1, buffer, bytes, c4d.COLORMODE_RGBf, c4d.PIXELCNT_0)
        except:
            return c4d.Vector()
        color = c4d.Vector(
            struct.unpack('f', buffer[:4])[0],
            struct.unpack('f', buffer[4:8])[0],
            struct.unpack('f', buffer[8:])[0]
        )
        return color

    def assignMatToObject(self, obj, mat, projection=c4d.TEXTURETAG_PROJECTION_SPHERICAL):
        """assigns a material to an object, or updates the material already assigned to an object
        Args:
            obj (BaseObject): object that will be assigned mat
            mat (BaseMaterial): material to assign to the object
            projection (int): material projection flag e.g. c4d.TEXTURETAG_PROJECTION_SPHERICAL
        Returns:
            (None)
        """
        tag = obj.GetTag(c4d.Ttexture)
        if tag is None:
            tag = obj.MakeTag(c4d.Ttexture)
        tag.__setitem__(c4d.TEXTURETAG_MATERIAL, mat)
        tag.__setitem__(c4d.TEXTURETAG_PROJECTION, projection)
        return

    def setCompositingTag(self, obj, camera, rays, gi):
        """finds (or creates) a compositing tag on an object and sets parameters
        Args:
            obj (BaseObject): object that owns the compositing tag
            camera (bool): set SEENBYCAMERA?
            rays (bool): set SEENBYRAYS?
            gi (bool): set SEENBYGI?
        Returns:
            (None)
        """
        tag = obj.GetTag(c4d.Tcompositing)
        if tag is None:
            tag = obj.MakeTag(c4d.Tcompositing)
        tag.__setitem__(c4d.COMPOSITINGTAG_SEENBYCAMERA, camera)
        tag.__setitem__(c4d.COMPOSITINGTAG_SEENBYRAYS, rays)
        tag.__setitem__(c4d.COMPOSITINGTAG_SEENBYGI, gi)
        return

    def findMaterial(self, doc, name, create=True):
        """Finds a material with the specified name
        Args:
            doc (BaseDocument): document to search
            name (str): material name to find
            create (bool): create the material if it is not found
        Returns:
            (BaseMaterial): found (or created) material, or None
        """
        mat = doc.GetFirstMaterial()
        while mat:
            # should we do a case insensitive match?
            if mat.GetName()==name:
                return mat
            mat = mat.GetNext()

        # no mat found... should we create it?
        if create:
            mat = c4d.BaseMaterial(c4d.Mmaterial)
            mat.SetName(name)
            doc.InsertMaterial(mat)
            return mat

        return None

    def findObject(self, doc, obj, otype, name, create=True):
        """Finds an object with the specified name
        Args:
            doc (BaseDocument): document that will be searched for object
            obj (BaseObject): starting object for search
            otype (int): object type to match (e.g. c4d.Osky)
            name (str): object name to find
            create (bool): create the object if it is not found
                    note: any recursive calls will set create to False,
                    so only the initial call will create an object if
                    the search doesn't find anything
        Returns:
            (BaseObject): found (or created) object, or None
        """
        while obj:
            # should we do a case insensitive match?
            if obj.GetType()==otype and obj.GetName()==name:
                return obj
            found = self.findObject(doc, obj.GetDown(), otype, name, False)
            if found is not None:
                return found
            obj = obj.GetNext()

        # no object found... should we create it?
        if create:
            obj = c4d.BaseObject(otype)
            obj.SetName(name)
            doc.InsertObject(obj)
            return obj

        return None

    def createBitmapShader(self, mat):
        """Creates a bitmap shader in a material's Luminance channel
        Args:
            mat (BaseMaterial): material that will have its Luminance channel set
        Returns:
            (BaseShader): shader that was created
        """
        shader = c4d.BaseShader(c4d.Xbitmap)
        mat.InsertShader(shader)
        mat.__setitem__(c4d.MATERIAL_LUMINANCE_SHADER, shader)
        return shader

    def setMatImage(self, mat, image_file):
        """Sets an image file to a material's Luminance channel and disables all other channels
        Args:
            mat (BaseMaterial): material that will have its Luminance channel set
            image_file (str): full path to image
        Returns:
            (None)
        """
        mat.__setitem__(c4d.MATERIAL_USE_COLOR, False)
        mat.__setitem__(c4d.MATERIAL_USE_DIFFUSION, False)
        mat.__setitem__(c4d.MATERIAL_USE_LUMINANCE, True)
        mat.__setitem__(c4d.MATERIAL_USE_TRANSPARENCY, False)
        mat.__setitem__(c4d.MATERIAL_USE_REFLECTION, False)
        mat.__setitem__(c4d.MATERIAL_USE_ENVIRONMENT, False)
        mat.__setitem__(c4d.MATERIAL_USE_FOG, False)
        mat.__setitem__(c4d.MATERIAL_USE_BUMP, False)
        mat.__setitem__(c4d.MATERIAL_USE_NORMAL, False)
        mat.__setitem__(c4d.MATERIAL_USE_ALPHA, False)
        mat.__setitem__(c4d.MATERIAL_USE_SPECULAR, False)
        mat.__setitem__(c4d.MATERIAL_USE_GLOW, False)
        mat.__setitem__(c4d.MATERIAL_USE_DISPLACEMENT, False)

        # get the luminance channel's shader if any
        shader = mat[c4d.MATERIAL_LUMINANCE_SHADER]
        if shader is None:
            # no shader? create a bitmap shader
            shader = self.createBitmapShader(mat)
        else:
            # we have a shader, is it a bitmap shader?
            if shader.GetType()!=c4d.Xbitmap:
                # no, remove whatever is there and create a new one
                shader.Remove()
                shader = self.createBitmapShader(mat)

        # we should have a bitmap shader now... let's set our image
        shader.__setitem__(c4d.BITMAPSHADER_FILENAME, image_file)

        return

    def getIBLValue(self, value):
        """Trys to match a value read from an ibl file to predefined regex patterns to cast it to the correct type
        Args:
            value (str): a value parsed from an ibl file
        Returns:
            (various): vector, string, float, or int representation of value
        """

        # note: the IBL_COLOR regex pattern is only looking for positive integers up to 3 digits.
        #       negative numbers, numbers longer than 3 digits, or numbers with decimal points will fail.
        color_match = IBL_COLOR.match(value)
        if color_match:
            # convert the color to a unit vector
            return c4d.Vector(
                float(color_match.group(1))/255.0,
                float(color_match.group(2))/255.0,
                float(color_match.group(3))/255.0
            )

        string_match = IBL_STRING.match(value)
        if string_match:
            return str(string_match.group(1))

        float_match = IBL_FLOAT.match(value)
        if float_match:
            return float(float_match.group(1))

        int_match = IBL_INT.match(value)
        if int_match:
            return int(int_match.group(1))

        # return original value if no regex is matched
        return value

    def parseIBL(self, filename):
        """Reads an ibl file (http://www.hdrlabs.com/sibl/) and organizes values into a dictionary (self._ibl_dict)
        Args:
            filename (str): the full path to an ibl file
        Returns:
            (bool): success or failure
        """

        # quick test on the file's extension
        if os.path.splitext(filename)[1].lower()!='.ibl':
            self._ibl_dict = None
            return False

        # initialize empty dictionary
        self._ibl_dict = {}
        with open(filename, 'r') as f:
            current_section = None
            for line in f.readlines():
                line = line.strip()

                # skip blank lines
                if len(line)==0:
                    continue

                # is this the start of a section?
                section_match = IBL_SECTION.match(line)
                if section_match:
                    current_section = section_match.group(1).lower()
                    # allow correct spelling of "environment" (ibl specs list "enviroment")
                    if current_section=='enviroment':
                        current_section = 'environment'
                    if not current_section in self._ibl_dict:
                        self._ibl_dict[current_section] = {}
                    continue

                # no current section? don't bother looking for values
                if current_section is None:
                    continue

                # is this line a value?
                value_match = IBL_VALUE.match(line)
                if value_match:
                    key = value_match.group(1).lower()
                    val = value_match.group(2)
                    self._ibl_dict[current_section][key] = self.getIBLValue(val)
                    continue

        # did we collect any values? and if so was "header" present?
        if len(self._ibl_dict)==0 or not 'header' in self._ibl_dict:
            # nope... kill the dict and return False
            self._ibl_dict = None
            return False
        else:
            # we're good... inject the file path into the dictionary
            self._ibl_dict['header']['_path'] = os.path.dirname(filename)

        return True

    def mapSampleToSphere(self, u, v):
        """Generates 3d coordinate on a unit sphere based on uv coordinates
        Args:
            u (float): x position on unit square
            v (float): y position on unit square
        Returns:
            (vector): 3d coordinate on unit sphere
        """
        pv = 1.0-(2.0*(1.0-v))
        r = math.sqrt(1.0-(pv*pv))
        phi = u*PI2
        pu = r*math.cos(phi)
        pw = r*math.sin(phi)
        return c4d.Vector(pu,pv,pw)

    def getSample(self, horizon=0.0):
        """Gets a random sample in a unit square.
        Args:
            horizon (float): the lower vertical limit of v. 0.5 will map all samples to the upper hemisphere
        Returns:
            (tuple): u, v
        """
        u = random.random()
        v = ((1.0-horizon)*random.random())+horizon # lerp
        return (u, v)

    def getSphereUV(self, d):
        """Get uv coordinates based on spherical projection
        Args:
            d (vector): 3d point to sample
        Returns:
            (tuple): u, v
        """
        u = 0.0
        v = 0.0
        sq = math.sqrt(d.x*d.x + d.z*d.z)
        if (sq==0.0):
            u = 0.0
            if (d.y>0.0):
                v = 0.5
            else:
                v = -0.5
        else:
            u = math.acos(d.x/sq)/PI2
            if (d.z<0.0):
                u = 1.0-u;
            if (u<0.0):
                u += 1.0
            elif (u>1.0):
                u -= 1.0
            v = math.atan(d.y/sq)/PI
        v = 0.5-v
        return (u,v)

    def buildIBL(self, op, filename):
        """manages parsing the ibl file and creating objects and materials
        Args:
            op (GeListNode): the list node connected with this instance
            filename (str): the full path to an ibl file
        Returns:
            (None)
        """

        # TODO: clean up this function... maybe break it out into a few reusable functions

        # reset self._env_image
        if self._env_image is not None:
            self._env_image.FlushAll()
            self._env_image = None

        # parse ibl and build components
        if self.parseIBL(filename):
            root_path = self._ibl_dict['header']['_path']
            if not os.path.isdir(root_path):
                return

            doc = c4d.documents.GetActiveDocument()
            data = op.GetDataInstance()
            radius = data.GetReal(c4d.PYDOME_ENV_REAL_RADIUS, 2000.0)

            # set ibl info
            data.SetString(c4d.PYDOME_STATIC_NAME, self._ibl_dict['header'].get('name', ''))
            data.SetString(c4d.PYDOME_STATIC_AUTHOR, self._ibl_dict['header'].get('author', ''))
            data.SetString(c4d.PYDOME_STATIC_LINK, self._ibl_dict['header'].get('link', ''))
            data.SetString(c4d.PYDOME_STATIC_LOCATION, self._ibl_dict['header'].get('location', ''))
            data.SetString(c4d.PYDOME_STATIC_LAT, str(self._ibl_dict['header'].get('geolat', '')))
            data.SetString(c4d.PYDOME_STATIC_LON, str(self._ibl_dict['header'].get('geolong', '')))
            data.SetString(c4d.PYDOME_STATIC_DATE, self._ibl_dict['header'].get('date', ''))
            data.SetString(c4d.PYDOME_STATIC_TIME, self._ibl_dict['header'].get('time', ''))
            data.SetString(c4d.PYDOME_TEXT_COMMENT,  self._ibl_dict['header'].get('comment', ''))

            if data.GetBool(c4d.PYDOME_BOOL_BACKGROUND) and 'background' in self._ibl_dict:
                # build background
                bkg_img_file = self._ibl_dict['background'].get('bgfile', None)
                if bkg_img_file is not None:
                    bkg_img = os.path.join(root_path, bkg_img_file)
                else:
                    bkg_img = None
                # does the image exist?
                if bkg_img is not None and os.path.isfile(bkg_img):
                    bkg_sky = self.findObject(doc, doc.GetFirstObject(), c4d.Osky, OBJNAME_BKG, create=True)
                    bkg_mat = self.findMaterial(doc, MATNAME_BKG, create=True)
                    self.setMatImage(bkg_mat, bkg_img)
                    self.assignMatToObject(bkg_sky, bkg_mat, c4d.TEXTURETAG_PROJECTION_SPHERICAL)
                    self.setCompositingTag(bkg_sky, camera=True, rays=False, gi=False)

            if 'environment' in self._ibl_dict:
                # build environment
                env_img_file = self._ibl_dict['environment'].get('evfile', None)
                if env_img_file is not None:
                    env_img = os.path.join(root_path, env_img_file)
                else:
                    env_img = None
                # does the image exist?
                if env_img is not None and os.path.isfile(env_img):
                    if data.GetBool(c4d.PYDOME_BOOL_ENVIRONMENT):
                        env_sky = self.findObject(doc, doc.GetFirstObject(), c4d.Osky, OBJNAME_ENV, create=True)
                        env_mat = self.findMaterial(doc, MATNAME_ENV, create=True)
                        self.setMatImage(env_mat, env_img)
                        self.assignMatToObject(env_sky, env_mat, c4d.TEXTURETAG_PROJECTION_SPHERICAL)
                        self.setCompositingTag(env_sky, camera=False, rays=False, gi=True)
                    # load the env image into a BaseBitmap (self._env_image)
                    self._env_image = bitmaps.BaseBitmap()
                    self._env_image.InitWith(env_img)

            if data.GetBool(c4d.PYDOME_BOOL_REFLECTION) and 'reflection' in self._ibl_dict:
                # build reflection
                ref_img_file = self._ibl_dict['reflection'].get('reffile', None)
                if ref_img_file is not None:
                    ref_img = os.path.join(root_path, ref_img_file)
                else:
                    ref_img = None
                # does the image exist?
                if ref_img is not None and os.path.isfile(ref_img):
                    ref_sky = self.findObject(doc, doc.GetFirstObject(), c4d.Osky, OBJNAME_REF, create=True)
                    ref_mat = self.findMaterial(doc, MATNAME_REF, create=True)
                    self.setMatImage(ref_mat, ref_img)
                    self.assignMatToObject(ref_sky, ref_mat, c4d.TEXTURETAG_PROJECTION_SPHERICAL)
                    self.setCompositingTag(ref_sky, camera=False, rays=True, gi=False)

            if data.GetBool(c4d.PYDOME_BOOL_SUN) and 'sun' in self._ibl_dict:
                # build dun
                sun_light = c4d.BaseObject(c4d.Olight)
                sun_light.SetName('sun')
                sun_light.__setitem__(c4d.LIGHT_TYPE, c4d.LIGHT_TYPE_DISTANT)
                sun_light.__setitem__(c4d.LIGHT_SHADOWTYPE, c4d.LIGHT_SHADOWTYPE_AREA)
                sun_light.__setitem__(c4d.LIGHT_AREADETAILS_INFINITE_ANGLE, c4d.utils.Rad(5.0))
                sun_light.__setitem__(c4d.LIGHT_COLOR, self._ibl_dict['sun'].get('suncolor', c4d.Vector(1.0)))
                sun_light.__setitem__(c4d.LIGHT_BRIGHTNESS, self._ibl_dict['sun'].get('sunmulti', 1.0))
                d = self.mapSampleToSphere(self._ibl_dict['sun'].get('sunu', 0.0),1.0-self._ibl_dict['sun'].get('sunv', 0.0))
                sun_light.SetAbsPos(d*radius)
                sun_light.SetAbsRot(c4d.utils.VectorToHPB(-d))
                doc.InsertObject(sun_light)

            if data.GetBool(c4d.PYDOME_BOOL_LIGHTS):
                # since there's no "lights" header, but rather "light1", "light2", etc we need to do a little more work
                for key in self._ibl_dict:
                    light_match = IBL_LIGHT.match(key)
                    if light_match:
                        index = int(light_match.group(1))
                        # build light
                        light = c4d.BaseObject(c4d.Olight)
                        light.SetName(self._ibl_dict[key].get('lightname', 'light%03d' % index))
                        light.__setitem__(c4d.LIGHT_TYPE, c4d.LIGHT_TYPE_DISTANT)
                        light.__setitem__(c4d.LIGHT_SHADOWTYPE, c4d.LIGHT_SHADOWTYPE_AREA)
                        light.__setitem__(c4d.LIGHT_AREADETAILS_INFINITE_ANGLE, c4d.utils.Rad(5.0))
                        light.__setitem__(c4d.LIGHT_COLOR, self._ibl_dict[key].get('lightcolor', c4d.Vector(1.0)))
                        light.__setitem__(c4d.LIGHT_BRIGHTNESS, self._ibl_dict[key].get('lightmulti', 1.0))
                        d = self.mapSampleToSphere(self._ibl_dict[key].get('lightu', 0.0),1.0-self._ibl_dict[key].get('lightv', 0.0))
                        light.SetAbsPos(d*radius)
                        light.SetAbsRot(c4d.utils.VectorToHPB(-d))
                        doc.InsertObject(light)

        return

    def Message(self, op, type, data):
        if type==c4d.MSG_DESCRIPTION_COMMAND:
            if data['id'][0].id==c4d.PYDOME_BTN_LOAD:
                ret = storage.LoadDialog(title='select ibl file', def_path=self._last_path)
                if ret is not None:
                    self._last_path = os.path.dirname(ret)
                    self.buildIBL(op, ret)

        return True

    def Init(self, op):
        data = op.GetDataInstance()
        data.SetBool(c4d.PYDOME_ENV_BOOL_USESRGB, False)
        data.SetLong(c4d.PYDOME_ENV_LONG_SEED, 0)
        data.SetReal(c4d.PYDOME_ENV_REAL_RADIUS, 2000.0)
        data.SetLong(c4d.PYDOME_ENV_LONG_SAMPLES, 100)
        data.SetBool(c4d.PYDOME_ENV_BOOL_NORMALIZE, True)
        data.SetReal(c4d.PYDOME_ENV_REAL_THRESHOLD, 0.0)
        data.SetReal(c4d.PYDOME_ENV_REAL_HORIZON, 0.5)
        return True

    def GetVirtualObjects(self, op, hh):
        if self._env_image is None:
            return None

        data = op.GetDataInstance()

        sRGB = data.GetBool(c4d.PYDOME_ENV_BOOL_USESRGB, False)
        seed = data.GetLong(c4d.PYDOME_ENV_LONG_SEED, 0)
        radius = data.GetReal(c4d.PYDOME_ENV_REAL_RADIUS, 2000.0)
        samples = data.GetLong(c4d.PYDOME_ENV_LONG_SAMPLES, 100)
        normalize = data.GetBool(c4d.PYDOME_ENV_BOOL_NORMALIZE, True)
        threshold = data.GetReal(c4d.PYDOME_ENV_REAL_THRESHOLD, 0.0)
        horizon = data.GetReal(c4d.PYDOME_ENV_REAL_HORIZON, 0.5)
        multiplier = data.GetReal(c4d.PYDOME_ENV_REAL_MULTIPLIER, 1.0);
        prototype = data.GetLink(c4d.PYDOME_ENV_LINK_PROTOTYPE)

        random.seed(seed)
        parent = c4d.BaseObject(c4d.Onull)

        # max and total intensities are used for normalization
        max_intensity = 0.0
        total_intensity = 0.0

        # this value will get multiplied against each light so that the total brightness = 1.0 (if we choose to normalize)
        normalize_multiply = 1.0

        lights = []

        parent.SetName('env.lights')

        # collect light information
        for i in range(samples):
            # try up to 100 times to get a sample in the range specified by "threshold"
            #   this can probably be updated to be user configurable
            for k in range(100):
                sample = self.getSample(horizon)
                d = self.mapSampleToSphere(sample[0], sample[1])
                uv = self.getSphereUV(d)
                color = self.getEnvColor(uv)
                # convert to sRGB (approximation of gamma=2.2)?
                if sRGB:
                    color.x = math.pow(color.x, 1.0/2.2)
                    color.y = math.pow(color.y, 1.0/2.2)
                    color.z = math.pow(color.z, 1.0/2.2)
                intensity = color.GetLength() # c4d.utils.VectorGray(color)
                if intensity>=threshold: # we can keep this sample... break the loop
                    break
            if intensity<threshold:
                # after 100 tries we're still below the threshold - skip this sample
                continue

            color.Normalize() # we already have intensity... safe to normalize

            light_info = {
                'color':     color,
                'intensity': intensity,
                'position':  d*radius,
                'rotation':  c4d.utils.VectorToHPB(-d),
            }
            # we need to do some processing before creating the lights, so just add the list info to a list for now
            lights.append(light_info)

            total_intensity += intensity
            if intensity>max_intensity:
                max_intensity = intensity

        if normalize and len(lights):
            normalize_multiply = (1.0/(total_intensity/float(len(lights))))/float(len(lights))

        total = 0.0
        for l in reversed(lights):
            if prototype is not None:
                light = prototype.GetClone(c4d.COPYFLAGS_0)
            else:
                light = c4d.BaseObject(c4d.Olight)
                light.__setitem__(c4d.LIGHT_TYPE, c4d.LIGHT_TYPE_SPOT)
                light.__setitem__(c4d.LIGHT_SHADOWTYPE, c4d.LIGHT_SHADOWTYPE_SOFT)
                light.__setitem__(c4d.LIGHT_DETAILS_SPECULAR, False)
            light.SetName('light.%03d' % (lights.index(l)+1))
            light.__setitem__(c4d.LIGHT_COLOR, l['color'])
            light.__setitem__(c4d.LIGHT_BRIGHTNESS, l['intensity']*normalize_multiply*multiplier)
            light.__setitem__(c4d.ID_BASEOBJECT_GENERATOR_FLAG, True)
            light.SetAbsPos(l['position'])
            light.SetAbsRot(l['rotation'])
            light.InsertUnder(parent)

        return parent

    def Read(self, node, hf, level):
        if level>=0:
            self._last_path = hf.ReadFilename()
            self._ibl_dict = pickle.loads(hf.ReadData())

        # reload env image from stored dictionary
        # TODO: cleanup redundant code (lots of this is also in buildIBL)
        if len(self._ibl_dict):
            ibl_root = self._ibl_dict['header'].get('_path', None)
            if ibl_root is not None and os.path.isdir(ibl_root) and 'environment' in self._ibl_dict:
                img_file = self._ibl_dict['environment'].get('evfile', None)
                if img_file is not None:
                    env_img = os.path.join(ibl_root, img_file)
                    if os.path.isfile(env_img):
                        if self._env_image is not None:
                            self._env_image.FlushAll()
                            self._env_image = None
                        self._env_image = bitmaps.BaseBitmap()
                        self._env_image.InitWith(env_img)

        return True

    def Write(self, node, hf):
        hf.WriteFilename(self._last_path)
        hf.WriteData(pickle.dumps(self._ibl_dict))

        return True

    def CopyTo(self, dest, snode, dnode, flags, trn):
        dest._env_image = self._env_image
        dest._last_path = self._last_path
        dest._ibl_dict = self._ibl_dict
        return True

if __name__=='__main__':
    print '-- pyDome %s' % PYDOME_BUILD_DATE
    print '--  Christopher Montesano'
    print '--  http://www.cmstuff.com'
    print '--  https://github.com/cmontesano/pyDome'

    path = os.path.dirname(__file__)
    bmp = bitmaps.BaseBitmap()
    bmp.InitWith(os.path.join(path, 'res', 'pyDome.tif'))
    plugins.RegisterObjectPlugin(
        id=PLUGIN_ID,
        str="PyDome",
        g=PyDome,
        description="Opydome",
        info=c4d.OBJECT_GENERATOR,
        icon=bmp,
        disklevel=0
    )
