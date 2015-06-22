#!/usr/bin/python3

"""
Główny moduł YTFS. Uruchomienie modułu powoduje zamontowanie systemu plików YTFS w zadanym katalogu.
"""

import os
import sys
import stat
import errno
import math
from enum import Enum
from copy import deepcopy
from time import time
from argparse import ArgumentParser
from functools import wraps

from fuse import FUSE, FuseOSError, Operations

#from stor import YTStor
from actions import YTActions, YTStor

class fd_dict(dict):

    """Rozszerzenie słownika, które znajduje najniższy niewykorzystany deskryptor i wpisuje podeń obiekt YTStor."""

    def push(self, yts):

        """
        Znajdź, dodaj i zwróć nowy deskryptor pliku.

        Parameters
        ----------
        yts : YTStor-obj or None
            Obiekt YTStor, dla którego chcemy przydzielić deskryptor lub None, jeśli alokujemy deskryptor dla
            pliku sterującego.
     
        Returns
        -------
        k : int
            Deskryptor do pliku.
        """

        if not isinstance(yts, (YTStor, type(None))):
            raise TypeError("Expected YTStor object or None.")

        k = 0
        while k in self.keys():
            k += 1
        self[k] = yts

        return k


class YTFS(Operations):

    """
    Główna klasa YTFS.

    Attributes
    ----------
    st : dict
        Słownik przechowujący podstawowe atrybuty plików. Zobacz: ``man 2 stat``.
    searches : dict
        Słownik będący głównym interfejsem do przechowywanych przez system plików danych o poszczególnych
        wyszukiwaniach i ich wynikach, czyli filmach. Struktura:
        
          searches = {
              'wyszukiwana fraza 1':  YTActions({
                                       'tytul1': <YTStor obj>,
                                       'tytul2': <YTStor obj>,
                                       ...
                                      }),
              'wyszukiwana fraza 2':  YTActions({ ... }),
              ...
          }
        
        Obiekt YTStor przechowuje wszystkie potrzebne informacje o filmie, nie tylko dane multimedialne.

        Uwaga: dla uproszczenia rozszerzenia w nazwach plików są obecne wyłącznie podczas wypisywania zawartości
        katalogu. We wszelkich innych operacjach są upuszczane.
    fds : fd_dict
        Słownik fd_dict wiążący będące w użyciu deskryptory z obiektami YTStor.
        Klucz: deskryptor;
        Wartość: obiekt YTStor dla danego pliku.
    __sh_script : bytes
        Zawartość zwracana przy odczycie pliku sterującego (pusty skrypt). System ma mieć wrażenie, że coś wykonał.
        Faktyczną operacją zajmuje się sam YTFS podczas otwarcia pliku sterującego.
    """

    st = {

        'st_mode': stat.S_IFDIR | 0o555,
        'st_ino': 0,
        'st_dev': 0,
        'st_nlink': 2,
        'st_uid': os.getuid(),
        'st_gid': os.getgid(),
        'st_size': 4096,
        'st_blksize': 512,
        'st_atime': 0,
        'st_mtime': 0,
        'st_ctime': 0
    }

    __sh_script = b"#!/bin/sh\n"

    def __init__(self, av):

        """Inicjalizacja obiektu"""

        self.searches = dict()
        self.fds = fd_dict()

        YTStor._setDownloadManner(av)

    class PathType(Enum):

        """
        Czytelna reprezentacja typu podanego identyfikatora krotkowego.

        Attributes
        ----------
        invalid : int
            Ścieżka nieprawidłowa
        main : int
            Katalog główny
        subdir : int
            Podkatalog (katalog wyszukiwania)
        file : int
            Plik (wynik wyszukiwania)
        ctrl : int
            Plik kontrolny
        """

        invalid = 0
        main = 1
        subdir = 2
        file = 3
        ctrl = 4

        @staticmethod
        def get(p):

            """
            Sprawdź typ ścieżki

            Parameters
            ----------
            p : str or tuple
                Ścieżka do pliku lub identyfikator krotkowy

            Returns
            -------
            path_type : PathType
                Typ pliku jako enumerator PathType

            """

            try:
                p = YTFS._YTFS__pathToTuple(p) #próba konwersji, jeśli p jest stringiem. inaczej nic się nie stanie
            except TypeError:
                pass

            if not isinstance(p, tuple) or len(p) != 2 or not (isinstance(p[0], (str, type(None))) and isinstance(p[1], (str, type(None)))):
                return YTFS.PathType.invalid

            elif p[0] is None and p[1] is None:
                return YTFS.PathType.main

            elif p[0] and p[1] is None:
                return YTFS.PathType.subdir

            elif p[0] and p[1]:
                
                if p[1][0] == ' ':
                    return YTFS.PathType.ctrl
                else:
                    return YTFS.PathType.file

            else:
                return YTFS.PathType.invalid

    def __pathToTuple(self, path):

        """
        Konwersja ścieżki do katalogu lub pliku na jego identyfikator krotkowy.

        Parameters
        ----------
        path : str
            Ścieżka do skonwertowania. Może przybrać postać /, /katalog, /katalog/ lub /katalog/nazwa_pliku.

        Returns
        -------
        tup_id : tuple
            Dwuelementowy krotkowy identyfikator katalogu/pliku postaci (katalog, nazwa_pliku). Jeśli ścieżka prowadzi
            do katalogu głównego, to oba pola krotki przyjmą wartość None. Jeśli ścieżka prowadzi do katalogu
            wyszukiwania, to pole nazwa_pliku przyjmie wartość None.

        Raises
        ------
        ValueError
            W przypadku podania nieprawidłowej ścieżki
        """

        if not path or path.count('/') > 2:
            raise ValueError("Bad path given") #pusta ścieżka "" albo zbyt głęboka

        try:
            split = path.split('/')
        except (AttributeError, TypeError):
            raise TypeError("Path has to be string") #path nie jest stringiem

        if split[0]:
            raise ValueError("Path needs to start with '/'") #path nie zaczyna się od "/"
        del split[0]

        try:
            if not split[-1]: split.pop() #podana ścieżka kończyła się ukośnikiem
        except IndexError:
            raise ValueError("Bad path given") #przynajmniej jeden element w split powinien na tę chwilę istnieć

        if len(split) > 2:
            raise ValueError("Path is too deep. Max allowed level i 2") #ścieżka jest zbyt długa

        try:
            d = split[0]
        except IndexError:
            d = None
        try:
            f = split[1]
        except IndexError:
            f = None

        if not d and f:
            raise ValueError("Bad path given") #jest nazwa pliku, ale nie ma katalogu #przypał

        return (d, f)

    def __exists(self, p):

        """
        Sprawdź czy plik o podanej ścieżce istnieje.

        Parameters
        ----------
        p : str or tuple
            Ścieżka do pliku

        Returns
        -------
        exists : bool
            True, jeśli plik istnieje. W przeciwnym razie False.

        """

        try:
            p = self.__pathToTuple(p)
        except TypeError:
            pass

        return ((not p[0] and not p[1]) or (p[0] in self.searches and not p[1]) or (p[0] in self.searches and
            p[1] in self.searches[p[0]]))

    def _pathdec(method):

        """
        Dekorator podmieniający argument path z reprezentacji tekstowej na identyfikator krotkowy.

        Parameters
        ----------
        method : function
            Funkcja/metoda do udekorowania.

        Returns
        -------
        mod : function
            Funckja/metoda po dekoracji.
        """

        @wraps(method) # functools.wraps umożliwia poprawną autogenerację dokumentacji dla udekorowanych funkcji.
        def mod(self, path, *args):

            try:
                return method(self, self.__pathToTuple(path), *args)

            except ValueError:
                raise FuseOSError(errno.EINVAL)

        return mod

    @_pathdec
    def getattr(self, tid, fh=None):

        """
        Atrybuty pliku.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        fh : int
            Deskryptor pliku. Nie jest konieczny, dlatego jest ignorowany.

        Returns
        -------
        st : dict
            Słownik zawierający atrybuty pliku. Zobacz: ``man 2 stat``.
        """

        if not self.__exists(tid):
            raise FuseOSError(errno.ENOENT)

        pt = self.PathType.get(tid)

        st = deepcopy(self.st)
        st['st_atime'] = int(time())
        st['st_mtime'] = st['st_atime']
        st['st_ctime'] = st['st_atime']

        if pt is self.PathType.file:
            
            st['st_mode'] = stat.S_IFREG | 0o444
            st['st_nlink'] = 1

            st['st_size'] = self.searches[ tid[0] ][ tid[1] ].filesize

        elif pt is self.PathType.ctrl:

            st['st_mode'] = stat.S_IFREG | 0o555 #te uprawnienia chyba trzeba ciutkę podreperować (FIXME?)
            st['st_nlink'] = 1
            st['st_size'] = len(self.__sh_script)

        st['st_blocks'] = math.ceil(st['st_size'] / st['st_blksize'])

        return st

    @_pathdec
    def readdir(self, tid, fh):

        """
        Listowanie katalogu. Wypisuje widoczne elementy obiektu `YTActions`.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        fh : int
            Deskryptor pliku. Pomijany w ciele funkcji.

        Returns
        -------
        list
            Lista nazw plików, która zostanie wyświetlona jako zawartość katalogu.
        """

        ret = []
        pt = self.PathType.get(tid)
        try:
            if pt is self.PathType.main:
                ret = list(self.searches)

            elif pt is self.PathType.subdir:
                ret = list(self.searches[tid[0]])

            elif pt is self.PathType.file:
                raise FuseOSError(errno.ENOTDIR)

            else:
                raise FuseOSError(errno.ENOENT)

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return ['.', '..'] + ret

    @_pathdec
    def mkdir(self, tid, mode):

        """
        Utworzenie katalogu.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        mode : int
            Ignorowany.
        """

        pt = self.PathType.get(tid)

        if pt is self.PathType.invalid or pt is self.PathType.file:
            raise FuseOSError(errno.EPERM)

        if self.__exists(tid):
            raise FuseOSError(errno.EEXIST)

        self.searches[tid[0]] = YTActions(tid[0])
        self.searches[tid[0]].updateResults()

        return 0

    @_pathdec
    def rename(self, old, new):

        """
        Zmiana nazwy katalogu. Potrzebne z uwagi na to, że wiele menadżerów plików tworzy katalog z domyślną nazwą,
        co uniemożliwia dokonania wyszukiwania bez użycia cli. Nie dopuszczamy możliwości zmiany nazwy pliku.

        Parameters
        ----------
        old : str
            Stara nazwa. Konwertowana przez _pathdec do postaci identyfikatora krotkowego.
        new : str
            Nowa nazwa. Konwertowana do postaci identyfikatora krotkowego dopiero we właściwym ciele funkcji.
        """

        new = self.__pathToTuple(new) # new też należy skonwertować.

        if not self.__exists(old):
            raise FuseOSError(errno.ENOENT)

        if self.PathType.get(old) is not self.PathType.subdir or self.PathType.get(new) is not self.PathType.subdir:
            raise FuseOSError(errno.EPERM)

        if self.__exists(new):
            raise FuseOSError(errno.EEXIST)

        self.searches[new[0]] = YTActions(new[0])
        self.searches[new[0]].updateResults()
        
        try:
            del self.searches[old[0]]

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return 0

    @_pathdec
    def rmdir(self, tid):

        """
        Usunięcie katalogu. Obiektowi YTActions leżącemu pod `tid` zleca się wyczyszczenie danych, a następnie usuwa.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        """

        pt = self.PathType.get(tid)

        if pt is self.PathType.main:
            raise FuseOSError(errno.EINVAL)
        elif pt is not self.PathType.subdir:
            raise FuseOSError(errno.ENOTDIR)

        try:
            self.searches[tid[0]].clean()
            del self.searches[tid[0]]

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return 0

    @_pathdec
    def unlink(self, tid):

        """
        Usunięcie pliku. Tak naprawdę nie usuwamy nic, ale żeby udało się usunąć katalog przez ``rm -r``, oszukujemy
        powłokę, że funkcja wykonała się prawidłowo.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        """

        return 0

    @_pathdec
    def open(self, tid, flags):

        """
        Otwarcie pliku. Obiekt YTStor przypisany do tego pliku jest inicjalizowany i wpisywany do słownika
        deskryptorów.
        
        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        flags : int
            Tryb otwarcia pliku. Zezwalamy tylko na odczyt.

        Returns
        -------
        int
            Nowy deskryptor pliku.
        """

        pt = self.PathType.get(tid)

        if pt is not self.PathType.file and pt is not self.PathType.ctrl:
            raise FuseOSError(errno.EINVAL)

        if flags & os.O_WRONLY or flags & os.O_RDWR:
            raise FuseOSError(errno.EROFS)

        if not self.__exists(tid):
            raise FuseOSError(errno.ENOENT)

        try:
            yts = self.searches[tid[0]][tid[1]]

            if yts.obtainInfo(): #FIXME bo brzydko
                fh = self.fds.push(yts)
                yts.registerHandler(fh)
                return fh #zwracamy deskryptor (powiązanie z YTStor)
            else:
                raise FuseOSError(errno.ENOENT) #FIXME? nie wiem czy pasi

        except KeyError:
            return self.fds.push(None) #zwracamy deskryptor (nie potrzeba żadnego powiązania dla pliku sterującego)

    @_pathdec
    def read(self, tid, length, offset, fh):

        """
        Odczyt z pliku. Dane uzyskiwane są od obiektu YTStor (zapisanego pod deskryptorem fh) za pomocą metody read.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Oryginalny argument `path` jest konwertowany przez dekorator `_pathdec` do postaci
            identyfikatora krotkowego.
        length : int
            Ilość danych do odczytania.
        offset : int
            Pozycja z której rozpoczynamy odczyt danych.
        fh : int
            Deskryptor pliku.

        Returns
        -------
        bytes
            Dane filmu.
        """

        try:
            return self.fds[fh].read(offset, length, fh)

        except AttributeError: #plik sterujący

            if tid[1] == " next":
                d = True
            elif tid[1] == " prev":
                d = False
            else:
                d = None

            try:
                self.searches[tid[0]].updateResults(d)
            except KeyError:
                raise FuseOSError(errno.EINVAL) #no coś nie pykło

            return self.__sh_script[offset:offset+length]

        except KeyError: #deskryptor nie istnieje
            raise FuseOSError(errno.EBADF)

    @_pathdec
    def release(self, tid, fh):

        """
        Zamknięcie pliku. Deskryptor pliku jest usuwany z self.fds.

        Parameters
        ----------
        tid : str
            Ścieżka do pliku. Ignorowana.
        fh : int
            Deskryptor pliku przeznaczony do zwolnienia.
        """

        try:
            del self.fds[fh]
        except KeyError:
            raise FuseOSError(errno.EBADF)

        return 0


def main(mountpoint, av):
    FUSE(YTFS(av), mountpoint, foreground=False)

if __name__ == '__main__':
    
    parser = ArgumentParser(description="YTFS - Youtube Filesystem: wyszukuj i odtwarzaj materiały z serwisu Youtube za pomocą operacji na plikach.", epilog="aby pobierać dźwięk oraz wideo należy połączyć flagi -a i -v.")
    parser.add_argument('mountpoint', type=str, nargs=1, help="punkt montowania")
    parser.add_argument('-a', action='store_true', default=False, help="pobieraj dźwięk (domyślne).")
    parser.add_argument('-v', action='store_true', default=False, help="pobieraj obraz")

    x = parser.parse_args()

    av = 0b00
    if x.a: av |= YTStor.DL_AUD
    if x.v: av |= YTStor.DL_VID

    main(x.mountpoint[0], av)
