from __future__ import absolute_import

import argparse
import cStringIO
import fnmatch
import os
import re
import struct
import textwrap
import sys
import mpyq
from itertools import groupby

from sc2reader import exceptions

LITTLE_ENDIAN,BIG_ENDIAN = '<','>'

class ReplayBuffer(object):
    """ The ReplayBuffer is a wrapper over the cStringIO object and provides
        convenience functions for reading structured data from Starcraft II
        replay files. These convenience functions can be sorted into several
        different categories providing an interface as follows:

        Stream Manipulation::
            tell(self)
            skip(self, amount)
            reset(self)
            align(self)
            seek(self, position, mode=SEEK_CUR)

        Data Retrieval::
            read_variable_int(self)
            read_string(self,additional)
            read_timestamp(self)
            read_count(self)
            read_data_structure(self)
            read_object_type(self, read_modifier=False)
            read_object_id(self)
            read_coordinate(self)
            read_bitmask(self)
            read_range(self, start, end)

        Basic Reading::
            read_byte(self)
            read_int(self, endian=LITTLE_ENDIAN)
            read_short(self, endian=LITTLE_ENDIAN)
            read_chars(self,length)
            read_hex(self,length)

        Core Reading::
            shift(self,bits)
            read(bytes,bits)

        The ReplayBuffer additionally defines the following properties:
            left
            length
            cursor
    """

    def __init__(self, file):
        #Accept file like objects and string objects
        if hasattr(file,'read'):
            self.io = cStringIO.StringIO(file.read())
        else:
            self.io = cStringIO.StringIO(file)

        # get length of stream
        self.io.seek(0, os.SEEK_END)
        self.length = self.io.tell()
        self.io.seek(0)

        # setup shift defaults
        self.bit_shift = 0
        self.last_byte = None

        #Extra optimization stuff
        self.lo_masks = [0x00, 0x01, 0x03, 0x07, 0x0F, 0x1F, 0x3F, 0x7F, 0xFF]
        self.lo_masks_inv = [0x00, 0x80, 0xC0, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF]
        self.hi_masks = [0xFF ^ mask for mask in self.lo_masks]
        self.hi_masks_inv = [0xFF ^ mask for mask in self.lo_masks_inv]
        self.coord_convert = [(2**(12 - i),1.0/2**i) for i in range(1,13)]

        self.read_basic = self.io.read
        self.char_buffer = cStringIO.StringIO()

        # Pre-generate the state for all reads, marginal time savings
        self.read_state = dict()
        for old in range(0,8):
            for new in range(0,8):
                self.read_state[(old,new)] = self.load_state(old, new)

    def load_state(self, old_bit_shift, new_bit_shift):
        old_bit_shift_inv = 8-old_bit_shift

        # Masks
        lo_mask = 2**old_bit_shift-1
        lo_mask_inv = 0xFF - 2**(8-old_bit_shift)+1
        hi_mask = 0xFF ^ lo_mask
        hi_mask_inv = 0xFF ^ lo_mask_inv

        #last byte parameters
        if new_bit_shift == 0: #this means we filled the last byte (8)
            last_mask = 0xFF
            adjustment = 8-old_bit_shift
            adjustment_mask = 2**adjustment-1
        else:
            last_mask = 2**new_bit_shift-1
            adjustment = new_bit_shift-old_bit_shift
            adjustment_mask = 2**adjustment-1

        return (old_bit_shift_inv, lo_mask, lo_mask_inv, hi_mask,
                hi_mask_inv, last_mask, adjustment, adjustment_mask)

    '''
        Additional Properties
    '''
    @property
    def left(self): return self.length - self.io.tell()
    @property
    def empty(self): return self.left==0
    @property
    def cursor(self): return self.io.tell()

    '''
        Stream manipulation functions
    '''
    def tell(self): return self.io.tell()
    def skip(self, amount): self.seek(amount, os.SEEK_CUR)
    def reset(self): self.io.seek(0); self.bit_shift = 0
    def align(self): self.bit_shift=0
    def seek(self, position, mode=os.SEEK_SET):
        self.io.seek(position, mode)
        if self.io.tell()!=0 and self.bit_shift!=0:
            self.io.seek(-1, os.SEEK_CUR)
            self.last_byte = ord(self.read_basic(1))

    def peek(self, length):
        start,last,ret = self.cursor,self.last_byte,self.read_hex(length)
        self.seek(start, os.SEEK_SET)
        self.last_byte = last
        return ret

    '''
        Read "basic" structures
    '''
    def read_byte(self):
        """ Basic byte read """
        if self.bit_shift==0:
            return ord(self.read_basic(1))
        else:
            return self.read(1)[0]

    def read_int(self, endian=LITTLE_ENDIAN):
        """ int32 read """
        chars = self.read_basic(4) if self.bit_shift==0 else self.read_chars(4)
        return struct.unpack(endian+'I', chars)[0]

    def read_short(self, endian=LITTLE_ENDIAN):
        """ short16 read """
        chars = self.read_basic(2) if self.bit_shift==0 else self.read_chars(2)
        return struct.unpack(endian+'H', chars)[0]

    def read_chars(self, length=0):
        if self.bit_shift==0:
            return self.read_basic(length)
        else:
            self.char_buffer.truncate(0)
            for byte in self.read(length):
                self.char_buffer.write(chr(byte))
            return self.char_buffer.getvalue()

    def read_hex(self, length=0):
        return self.read_chars(length).encode("hex")

    '''
        Read replay-specific structures
    '''
    def read_count(self):
        return self.read_byte()/2

    def read_variable_int(self):
        """ Blizzard VL integer """
        byte = self.read_byte()
        shift, value = 1,byte & 0x7F
        while byte & 0x80 != 0:
            byte = self.read_byte()
            value = ((byte & 0x7F) << shift * 7) | value
            shift += 1

        #The last bit of the result is a sign flag
        return pow(-1, value & 0x1) * (value >> 1)

    def read_string(self, length=None):
        """<length> ( <char>, .. ) as unicode"""
        return self.read_chars(length if length!=None else self.read_byte())

    def read_timestamp(self):
        """
        Timestamps are 1-4 bytes long and represent a number of frames. Usually
        it is time elapsed since the last event. A frame is 1/16th of a second.
        The least significant 2 bits of the first byte specify how many extra
        bytes the timestamp has.
        """
        first = self.read_byte()
        time,count = first >> 2, first & 0x03
        if count == 0:
            return time
        elif count == 1:
            return time << 8 | self.read_byte()
        elif count == 2:
            return time << 16 | self.read_short()
        elif count == 3:
            return time << 24 | self.read_short() << 8 | self.read_byte()
        else:
            raise ValueError()

    def read_data_struct(self):
        """
        Read a Blizzard data-structure. Structure can contain strings, lists,
        dictionaries and custom integer types.
        """
        #The first byte serves as a flag for the type of data to follow
        datatype = self.read_byte()
        if datatype == 0x02:
            #0x02 is a byte string with the first byte indicating
            #the length of the byte string to follow
            count = self.read_count()
            return self.read_string(count)

        elif datatype == 0x04:
            #0x04 is an serialized data list with first two bytes always 01 00
            #and the next byte indicating the number of elements in the list
            #each element is a serialized data structure
            self.skip(2)    #01 00
            return [self.read_data_struct() for i in range(self.read_count())]

        elif datatype == 0x05:
            #0x05 is a serialized key,value structure with the first byte
            #indicating the number of key,value pairs to follow
            #When looping through the pairs, the first byte is the key,
            #followed by the serialized data object value
            data = dict()
            for i in range(self.read_count()):
                count = self.read_count()
                key,value = count, self.read_data_struct()
                data[key] = value #Done like this to keep correct parse order
            return data

        elif datatype == 0x06:
            return self.read_byte()
        elif datatype == 0x07:
            return self.read_int()
        elif datatype == 0x09:
            return self.read_variable_int()

        raise TypeError("Unknown Data Structure: '%s'" % datatype)

    def read_object_type(self, read_modifier=False):
        """ Object type is big-endian short16 """
        type = self.read_short(endian=BIG_ENDIAN)
        if read_modifier:
            type = (type << 8) | self.read_byte()
        return type

    def read_object_id(self):
        """ Object ID is big-endian int32 """
        return self.read_int(endian=BIG_ENDIAN)

    def read_coordinate(self):
        # Combine coordinate whole and fraction
        def _coord_to_float(coord):
            fraction = 0
            for mask,quotient in self.coord_convert:
                if (coord[1] & mask):
                    fraction = fraction + quotient
            return coord[0] + fraction

        # Read an x or y coordinate dimension
        def _coord_dimension():
            coord = self.read(bits=20)
            return _coord_to_float([coord[0], coord[1] << 4 | coord[2],])

        # TODO?: Handle optional z dimension
        return (_coord_dimension(), _coord_dimension())

    def read_bitmask(self):
        """ Reads a bitmask given the current bitoffset """
        length = self.read_byte()
        bytes = reversed(self.read(bits=length))
        mask = 0
        for byte in bytes:
            mask = (mask << 8) | byte

        # Turn things like 10010011 into [True, False, False, True,...]
        def _make_mask(byte, bit_length, current=1):
            if current < bit_length:
                byte_mask = (2**(bit_length-current))
                bytes = [(byte & byte_mask) > 0,]
                return bytes + _make_mask(byte, bit_length, current+1)
            else:
                return [byte & 0x1 == 0x01,]

        return list(reversed(_make_mask(mask, length)))

    def read_range(self, start, end):
        current = self.cursor
        self.io.seek(start)
        ret = self.read_basic(end-start)
        self.io.seek(current)
        return ret


    '''
        Base read functions
    '''
    def shift(self, bits):
        """
        The only valid use of Buffer.shift is when you know that there are
        enough bits left in the loaded byte to accommodate your request.

        If there is no loaded byte, or the loaded byte has been exhausted,
        then Buffer.shift(8) could technically be used to read a single
        byte-aligned byte.
        """
        try:
            #declaring locals instead of accessing dict on multiple use seems faster
            bit_shift = self.bit_shift
            new_shift = bit_shift+bits

            #make sure there are enough bits left in the byte
            if new_shift <= 8:
                if not bit_shift:
                    self.last_byte = ord(self.read_basic(1))

                #using a bit_mask_array tested out to be 20% faster, go figure
                ret = (self.last_byte >> bit_shift) & self.lo_masks[bits]
                #using an if for the special case tested out to be faster, hrm
                self.bit_shift = 0 if new_shift == 8 else new_shift
                return ret

            else:
                msg = "Cannot shift off %s bits. Only %s bits remaining."
                raise ValueError(msg % (bits, 8-self.bit_shift))

        except TypeError:
            raise EOFError("Cannot shift requested bits. End of buffer reached")

    def read(self, bytes=0, bits=0):
        try:
            bytes, bits = bytes+bits/8, bits%8
            bit_count = bytes*8+bits

            #check special case of not having to do any work
            if bit_count == 0: return []

            #check sepcial case of intra-byte read
            if bit_count <= (8-self.bit_shift):
                return [self.shift(bit_count)]

            #check special case of byte-aligned reads, performance booster
            if self.bit_shift == 0:
                base = [ord(self.read_basic(1)) for byte in range(bytes)]
                if bits != 0:
                    return base+[self.shift(bits)]
                return base

            # Calculated shifts as our keys
            old_bit_shift = self.bit_shift
            new_bit_shift = (self.bit_shift+bits) % 8

            # Load the precalculated state variables
            (old_bit_shift_inv, lo_mask, lo_mask_inv,
             hi_mask, hi_mask_inv, last_mask, adjustment,
             adjustment_mask) = self.read_state[(old_bit_shift,new_bit_shift)]

            #Set up for the looping with a list, the bytes, and an initial part
            raw_bytes = list()
            prev, next = self.last_byte, ord(self.read_basic(1))
            first = prev & hi_mask
            bit_count -= old_bit_shift_inv

            while bit_count > 0:

                if bit_count <= 8: #this is the last byte
                    #The bits in the last byte are included in order starting at
                    #the new_bit_shift boundary with extra bits bumped back a byte
                    #because we can have odd bit requests, the bit shift can change
                    last = (next & last_mask)

                    # we need to bring the first byte closer
                    # if the adjustment is lower than 0
                    if adjustment < 0:
                        first = first >> abs(adjustment)
                        raw_bytes.append(first | last)
                    elif adjustment > 0:
                        raw_bytes.append(last & adjustment_mask)
                        raw_bytes.append(first | (last >> adjustment))
                    else:
                        raw_bytes.append(first | last)

                    bit_count = 0

                if bit_count > 8: #We can do simple wrapping for middle bytes
                    second = (next & lo_mask_inv) >> old_bit_shift_inv
                    raw_bytes.append(first | second)

                    #To remain consistent, always shfit these bits into the hi_mask
                    first = (next & hi_mask_inv) << old_bit_shift
                    bit_count -= 8

                    #Cycle down to the next byte
                    prev,next = next,ord(self.read_basic(1))

            self.last_byte = next
            self.bit_shift = new_bit_shift
            return raw_bytes

        except TypeError:
            raise EOFError("Cannot read requested bits/bytes. End of buffer reached")

class PersonDict(dict):
    """Delete is supported on the pid index only"""
    def __init__(self, *args, **kwargs):
        self._key_map = dict()

        if args:
            print args
            for arg in args[0]:
                self[arg[0]] = arg[1]

        if kwargs:
            print kwargs
            for key, value in kwargs.iteritems():
                self[key] = value

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._key_map[key]

        return super(PersonDict, self).__getitem__(key)

    def __setitem__(self, key, value):
        if isinstance(key, str):
            self._key_map[key] = value.pid
            key = value.pid
        elif isinstance(key, int):
            self._key_map[value.name] = key

        super(PersonDict, self).__setitem__(value.pid, value)


def windows_to_unix(windows_time):
    # This windows timestamp measures the number of 100 nanosecond periods since
    # January 1st, 1601. First we subtract the number of nanosecond periods from
    # 1601-1970, then we divide by 10^7 to bring it back to seconds.
    return (windows_time-116444735995904000)/10**7

import inspect
def key_in_bases(key,bases):
    bases = list(bases)
    for base in list(bases):
        bases.extend(inspect.getmro(base))
    for clazz in set(bases):
        if key in clazz.__dict__: return True
    return False

class AttributeDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError('No such attribute {0}'.format(name))

    def __setattr__(self, name, value):
        self[name] = value

    def copy(self):
        return AttributeDict(super(AttributeDict,self).copy())

class Color(AttributeDict):
    @property
    def hex(self):
        return "{0.r:02X}{0.g:02X}{0.b:02X}".format(self)

    def __str__(self):
        if not hasattr(self,'name'):
            self.name = COLOR_CODES[self.hex]
        return self.name

def open_archive(replay_file):
    # Don't read the listfile because some replays have corrupted listfiles
    # due  to tampering by 3rd parties.
    #
    # In order to wrap mpyq in exceptions we have to do this try hack.
    try:
        replay_file.seek(0)
        return  mpyq.MPQArchive(replay_file, listfile=False)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        trace = sys.exc_info()[2]
        raise exceptions.MPQError("Unable to construct the MPQArchive",e), None, trace

def extract_data_file(data_file, archive):
    # To wrap mpyq exceptions we have to do this try hack again.
    try:
        # Some sites tamper with the message events file so
        # Catch decompression errors and try again before giving up
        if data_file == 'replay.message.events':
            try:
                file_data = archive.read_file(data_file, force_decompress=True)
            except IndexError as e:
                if str(e) == "string index out of range":
                    file_data = archive.read_file(data_file, force_decompress=False)
                else:
                    raise
        else:
            file_data = archive.read_file(data_file)

        return file_data

    except KeyboardInterrupt:
        raise
    except Exception as e:
        trace = sys.exc_info()[2]
        raise exceptions.MPQError("Unable to extract file: {0}".format(data_file),e), None, trace

def read_header(replay_file):
    # Extract useful header information from the MPQ files. This information
    # can be used to configure the rest of the program to correctly parse
    # the archived data files.
    replay_file.seek(0)
    buffer = ReplayBuffer(replay_file)

    #Sanity check that the input is in fact an MPQ file
    if buffer.empty or buffer.read_hex(4).upper() != "4D50511B":
        raise exceptions.FileError("File '%s' is not an MPQ file" % file.name)

    max_data_size = buffer.read_int(LITTLE_ENDIAN)
    header_offset = buffer.read_int(LITTLE_ENDIAN)
    data_size = buffer.read_int(LITTLE_ENDIAN)

    #array [unknown,version,major,minor,build,unknown] and frame count
    header_data = buffer.read_data_struct()
    versions = header_data[1].values()
    frames = header_data[3]
    build = versions[4]
    release_string = "%s.%s.%s.%s" % tuple(versions[1:5])
    length = Length(seconds=frames/16)

    keys = ('versions', 'frames', 'build', 'release_string', 'length')
    return filter(lambda item: item[0] in keys, locals().iteritems())

def merged_dict(a, b):
    c = a.copy()
    c.update(b)
    return c

def sc2replay_ext(filename):
    name, ext = os.path.splitext(filename)
    return ext.lower() == ".sc2replay"

def get_replay_files(path, exclude=[], depth=-1, followlinks=False, **extras):
    #os.walk and os.path.isfile fail silently. We want to be loud!
    if not os.path.exists(path):
        raise ValueError("Location `{0}` does not exist".format(path))

    # os.walk can't handle file paths, only directories
    if os.path.isfile(path):
        return [path] if sc2replay_ext(path) else []

    files = list()
    for root, directories, filenames in os.walk(path, followlinks=followlinks):
        # Exclude the indicated directories by removing them from `directories`
        for directory in list(directories):
            if directory in exclude or depth == 0:
                directories.remove(directory)

        # Extend our return value only with the allowed file type and regex
        allowed_files = filter(sc2replay_ext, filenames)
        files.extend(os.path.join(root, filename) for filename in allowed_files)
        depth -= 1

    return files

from datetime import timedelta
class Length(timedelta):
    @property
    def hours(self):
        return self.seconds/3600

    @property
    def mins(self):
        return self.seconds/60

    @property
    def secs(self):
        return self.seconds%60

    def __str__(self):
        if self.hours:
            return "{0:0>2}.{1:0>2}.{2:0>2}".format(self.hours,self.mins,self.secs)
        else:
            return "{0:0>2}.{1:0>2}".format(self.mins,self.secs)

class RangeMap(dict):
    def add_range(self, start, end, reader_set):
        self.ranges.append((start, end, reader_set))

    def __init__(self):
        self.ranges = list()

    def __getitem__(self,key):
        for start, end, range_set in self.ranges:
            if end:
                if (start <= key < end):
                    return range_set
            else:
                if start <= key:
                    return range_set
        raise KeyError(key)

class Formatter(argparse.RawTextHelpFormatter):
    """FlexiFormatter which respects new line formatting and wraps the rest

    Example:
        >>> parser = argparse.ArgumentParser(formatter_class=FlexiFormatter)
        >>> parser.add_argument('a',help='''\
        ...     This argument's help text will have this first long line\
        ...     wrapped to fit the target window size so that your text\
        ...     remains flexible.
        ...
        ...         1. This option list
        ...         2. is still persisted
        ...         3. and the option strings get wrapped like this\
        ...            with an indent for readability.
        ...
        ...     You must use backslashes at the end of lines to indicate that\
        ...     you want the text to wrap instead of preserving the newline.
        ... ''')

    Only the name of this class is considered a public API. All the methods
    provided by the class are considered an implementation detail.
    """

    @classmethod
    def new(cls, **options):
        return lambda prog: Formatter(prog, **options)

    def _split_lines(self, text, width):
        lines = list()
        main_indent = len(re.match(r'( *)',text).group(1))
        # Wrap each line individually to allow for partial formatting
        for line in text.splitlines():

            # Get this line's indent and figure out what indent to use
            # if the line wraps. Account for lists of small variety.
            indent = len(re.match(r'( *)',line).group(1))
            list_match = re.match(r'( *)(([*-+>]+|\w+\)|\w+\.) +)',line)
            if(list_match):
                sub_indent = indent + len(list_match.group(2))
            else:
                sub_indent = indent

            # Textwrap will do all the hard work for us
            line = self._whitespace_matcher.sub(' ', line).strip()
            new_lines = textwrap.wrap(
                text=line,
                width=width,
                initial_indent=' '*(indent-main_indent),
                subsequent_indent=' '*(sub_indent-main_indent),
            )

            # Blank lines get eaten by textwrap, put it back with [' ']
            lines.extend(new_lines or [' '])

        return lines
